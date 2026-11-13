"""Check feed configuration and publication with private queue readings."""
from __future__ import annotations

from scripts.build_queue import admission_records
from scripts.build_queue.test_queue_view import with_converted_executions

import json
import io
import os
import re
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
from scripts.buildlog.mac_idle_test_support import write_idle_alert, write_idle_config
from scripts.build_queue import admission_client, admission_protocol as wire, cloud_progress, memory_feed, progress_profile, queue_lines, queue_view
from scripts.build_queue.test_queue_view import cloud_reading
from scripts.build_queue.admission_test_support import AdmissionFixture, NOW as ADMISSION_NOW, only_admitted
from scripts.build_queue.queue_fixture import write_process, write_scope
from scripts.build_queue.scope_activity import RunningServiceJob, IdleService, LiveBuildRun, NoLiveBuildRun, ScopeProcess, ScopeProcesses
from scripts.lint import test_memory_admit as memory_tests
from scripts.lib import conf, test_hang_guard
from scripts.lib.build_steps import read_step_names
from scripts.message import unit_labels
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture, write_config_fixture


class FeedStopped(Exception):
    """The fixture stops the feed after its recorded cycles."""


def captured_reading(*, running: tuple[admission_records.RunningStep, ...] = ()) -> admission_records.QueueRead:
    inputs = admission_records.QueueInputs([], running, (), admission_records.HostMemory(30 * admission_records.GIB, 60 * admission_records.GIB),
        admission_records.NO_PRESSURE_HOLD, admission_records.ReadyToStart(), admission_records.NoHeadAdmissionOverride(), 200_000)
    return gate.queue_read_from(inputs, admission_policy_fixture())


def with_queue_priority(reading: admission_records.QueueRead, units: tuple[str, ...] = ("research",)
                        ) -> admission_records.QueueRead:
    """Add a private user priority to the reading."""
    rows: list[admission_records.Waiter] = [row for _, row in reading.ordered]
    rows.extend(reservation["queue_waiter"] for reservation in reading.explanation.reservations if "queue_waiter" in reservation)
    priority = admission_records.QueuePriority(tuple(
        {"step": step, "unit": unit, "why": "the user's bump", "set_at": reading.explanation.now, "set_by": "fixture-user"}
        for unit in units
        for step in ([admission_records.priority_step(row) for row in rows if row.get("unit", "research") == unit]
                     or [admission_records.SelectedPriorityStep(1, "1", 0)])))
    return replace(reading, explanation=replace(reading.explanation, priority=priority))


def queued_reading(reading: admission_records.QueueRead, units: tuple[str, ...]) -> admission_records.QueueRead:
    """Supply an ordered queue with repeated units and an older normal step."""
    waiting = tuple(admission_records.WaitingStep(200 + index, f"wait-{index}", unit, "nextest",
        f"/fixture/{unit}", 100 - index * 10, admission_records.GIB, admission_records.NeedsMemory(), unit=unit)
        for index, unit in enumerate(("research", "research", "cables", "normal")))
    ordered: list[tuple[Path, admission_records.Waiter]] = [
        (Path(f"/fixture/{step.pid}"), {"pid": step.pid, "start_time": str(step.pid), "since": step.since,
                                     "need": step.need, "unit": step.unit}) for step in waiting]
    current = replace(reading, ordered=ordered, view=replace(reading.view, waiting=waiting))
    return with_queue_priority(current, units) if units else current


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


def reservation_reading(*, project: str = "fixture-project") -> admission_records.QueueRead:
    reservation = memory_tests.ProgressKeepAsideTests.reservation("nextest")
    reservation["queue_waiter"] = {"pid": reservation["pid"], "start_time": reservation["start_time"],
        "since": 0, "need": reservation["need"], "session_id": "session", "session_name": "fixture",
        "step": "nextest", "folder": reservation["worktree"]}
    memory = admission_records.MeasuredMemory(2 * admission_records.GIB, anonymous=admission_records.AnonymousRunUse(admission_records.GIB))
    inputs = memory_tests.ProgressKeepAsideTests.inputs((reservation,), (memory,))
    inputs = replace(inputs, running=(replace(inputs.running[0], project=project),))
    return gate.queue_read_from(inputs, admission_policy_fixture())


def with_wait_reason(reading: admission_records.QueueRead, reason: admission_records.WaitReason) -> admission_records.QueueRead:
    waiter = admission_records.WaitingStep(200, "fixture", "fixture", "nextest", "/fixture", 10,
                              admission_records.GIB, reason, project="fixture-project")
    row: admission_records.Waiter = {"pid": waiter.pid, "start_time": str(waiter.pid), "since": waiter.since,
        "need": waiter.need, "session_id": waiter.session_id, "session_name": waiter.session_name,
        "step": waiter.step, "folder": waiter.folder}
    return replace(reading, view=replace(reading.view, waiting=(waiter,)),
                   ordered=[(Path("/fixture/waiter"), gate.rank_waiter(row, admission_records.NormalOrder()))])


def leftover_step(minutes: int, *, small: bool = False, members: tuple[ScopeProcess, ...] = ()) -> admission_records.RunningStep:
    started_at = 200_000 - minutes * 60
    activity = ScopeProcesses(len(members) or 1, "fixture", "fixture", "/fixture", "python3", started_at,
                              True, NoLiveBuildRun(), members=members)
    memory = admission_records.UnownedSliceMemory(admission_records.GIB // 4 if small else admission_records.GIB,
        owner=f"leftover-{minutes}", activity=activity, pid=minutes)
    return admission_records.RunningStep("fixture", "fixture", f"leftover-{minutes}", "/fixture", 0,
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
        _ = (self.root / "mac-builds.json").write_text(json.dumps({"read_at": ADMISSION_NOW, "builds": [], "error": ""}))
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
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),
        })))
        _ = self.enterContext(mock.patch.object(admission_client, "view",
            side_effect=AssertionError("The test must supply a private queue reading")))
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={}))
        _ = self.enterContext(mock.patch.object(memory_feed, "host_name", return_value="fixture"))
        _ = self.enterContext(mock.patch.object(memory_feed, "PROC", self.root / "proc"))
        _ = self.enterContext(mock.patch.object(memory_feed, "CI_CGROUP", self.root / "ci"))
        _ = self.enterContext(mock.patch.object(memory_feed, "CGROUP", self.root / "cgroup"))
        _ = self.enterContext(mock.patch.object(charts, "page_data", return_value=report_fixture()))
        _ = self.enterContext(mock.patch.object(queue_view, "mac_reading_path", return_value=self.root / "mac-builds.json"))
        _ = self.enterContext(mock.patch.object(queue_view, "refresh_mac_builds"))

    def configure(self, *, seconds: float = 0.125) -> None:
        source = Path(__file__).resolve().parents[2] / "config/memory_feed.conf"
        text = source.read_text().replace("output_folder=~/.local/share/review-links/h7uj5c", f"output_folder={self.output}")
        text = text.replace("interval_seconds=3", f"interval_seconds={seconds}").replace("# range: 1..60", "# range: 5e-324..")
        _ = self.configuration.write_text(text)

    def expected_page(self, seconds: float = 0.125) -> str:
        return Path(memory_feed.__file__).with_name("memory_live.html").read_text().replace(
            "__MEMORY_FEED_INTERVAL_SECONDS__", json.dumps(seconds))

    def test_converted_rows_show_their_hosts_without_charging_remote_memory_locally(self) -> None:
        baseline = reservation_reading()
        reading = with_converted_executions(baseline)
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading()
        remote = [row for row in page["walk"] if "used" in row and "counted" in row and "reserved" in row and row.get("machine") in
                  ("fixture-mac", "fixture-ssh", "uncertain-mac")]
        self.assertEqual(len(remote), 3)
        self.assertEqual({row.get("machine") for row in remote}, {"fixture-mac", "fixture-ssh", "uncertain-mac"})
        self.assertTrue(all(row["counted"] == 0 and row["reserved"] == 0 for row in remote))
        self.assertTrue(all(isinstance(row["used"], admission_records.RunUseUnavailable) for row in remote))
        uncertain, = [row for row in remote if row.get("machine") == "uncertain-mac"]
        self.assertIsInstance(uncertain["pid"], admission_records.PidNotRecorded)
        self.assertEqual(uncertain["state"], "uncertain")
        self.assertEqual(reading.explanation.room, baseline.explanation.room)

    def test_step_table_keeps_assignment_fields_with_report_tooltips(self) -> None:
        reading = cloud_reading(with_wait_reason(reservation_reading(), admission_records.StartSpacingWait(110)))
        with (mock.patch.object(admission_client, "view", return_value=reading),
              mock.patch.object(os, "cpu_count", return_value=4)):
            page = memory_feed.reading()
        payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
        html = self.expected_page()
        functions = html[html.index("const TIPS ="):html.index("const tip =")]
        constants = html[html.index("const f1 ="):html.index("function setSeg(")]
        render = html[html.index("function renderWalk("):html.index("function fitAxisLabels(")]
        escape = html[html.index("function esc("):html.index("let lastRead =")]
        javascript = functions + constants + render + escape + """
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {style:{}, classList:{toggle(){}}, innerHTML:"",
    insertAdjacentHTML(position, html) {
      if (position !== "beforeend") throw new Error("unexpected insert position");
      this.innerHTML += html;
    }});
  return elements.get(id);
};
function fitLabels() {}
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
renderWalk(data);
process.stdout.write(JSON.stringify({
  walk:data.walk.map((row,index)=>tipText('row:'+index,data)),
  waiters:data.waiters.map((row,index)=>tipText('wait:'+index,data)),
  table:$("walk").innerHTML}));
"""
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        result = subprocess.run([executable, "-e", javascript], input=json.dumps(payload), text=True,
            capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rendered = cast(dict[str, object], json.loads(result.stdout))
        tips = {key: cast(list[list[str]], rendered[key]) for key in ("walk", "waiters")}
        table = cast(str, rendered["table"])
        matches = cast(list[tuple[str, str, str]],
            re.findall(r'<tr[^>]*data-tip="(row|wait):([0-9]+)"[^>]*>(.*?)</tr>', table, flags=re.S))
        cells = {f"{kind}:{index}": body for kind, index, body in matches}
        for collection, kind in (("walk", "row"), ("waiters", "wait")):
            rows = cast(list[dict[str, object]], payload[collection])
            local_position = 0
            for index, row in enumerate(rows):
                if kind == "row" and not (row.get("queued") or row.get("kind") == "cloud"):
                    continue
                if kind == "wait" and row.get("kind") != "cloud":
                    local_position += 1
                with self.subTest(kind=kind, pid=row["pid"]):
                    text = "\n".join(tips[collection][index])
                    cell = cells[f"{kind}:{index}"]
                    progress = cast(dict[str, object], row.get("progress", {}))
                    place = (str(progress.get("phase", "—")) if kind == "row" else
                             "cloud" if row.get("kind") == "cloud" else str(local_position))
                    self.assertIn(f'<td class="place">{place}</td>', cell)
                    self.assertIn(f'<td class="pid">{row["pid"]}</td>', cell)
                    machine = str(row.get("machine") or "")
                    self.assertIn(f'<td class="machine" data-machine="{machine}">{machine}</td>', cell)
                    if kind == "wait":
                        self.assertEqual(machine, "")
                    else:
                        self.assertTrue(machine)
                    cpus = row.get("vcpus")
                    shown_cpus = str(cpus) if kind == "row" and isinstance(cpus, int) else "—" if kind == "row" else ""
                    self.assertIn(f'<td class="vcpus">{shown_cpus}</td>', cell)
                    unit = str(row["unit"])
                    self.assertIn(f'<td class="unit"><span class="cell-text">{unit}</span></td>', cell)
                    self.assertIn(str(row["name"]), text)
                    if row.get("kind") == "cloud":
                        for field in ("used", "counted", "reserved", "need"):
                            self.assertNotIn(field, row)
                        for detail in str(row.get("status") or row.get("reason")).split(" · "):
                            self.assertIn(detail, text)
                        if kind == "row":
                            self.assertIn("This work reserves no natedev memory.", text)
                            self.assertIn(f"Machine: {machine}", text)
                        else:
                            self.assertIn("This step needs a remote execution slot.", text)

    def test_ci_row_preserves_a_failed_memory_reading(self) -> None:
        diagnostic = "CI memory reading failed: permission denied"
        cgroup = self.root / "ci"
        cgroup.mkdir()
        step = admission_records.RunningStep("", "hana-ci.slice", "service", str(cgroup), 0,
            admission_records.UnownedSliceMemory(0, cgroup=str(cgroup), owner="hana-ci.slice",
                pid=701, diagnostic=diagnostic, job_state=RunningServiceJob(199_000)), pid=701)
        reading = captured_reading(running=(step,))
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading()
        row, = [row for row in page["walk"] if row.get("ci")]
        self.assertEqual(row.get("diagnostic"), diagnostic)

    def test_ci_memory_stays_in_the_taken_total_with_an_idle_or_running_job(self) -> None:
        cgroup = self.root / "ci"
        (cgroup / "github-runner-fixture.service").mkdir(parents=True)
        for busy in (False, True):
            with self.subTest(busy=busy):
                runner = {"unit": "github-runner-fixture.service", "name": "fixture",
                          "state": "busy" if busy else "idle", "used": 0.125, "pid": 701,
                          "started_at": 199_000, "job": "tests"}
                step = admission_records.RunningStep("", "hana-ci.slice", "service", str(cgroup), 0,
                    admission_records.UnownedSliceMemory(admission_records.GIB // 8,
                        cgroup=str(cgroup), owner="hana-ci.slice", pid=701,
                        job_state=RunningServiceJob(199_000) if busy else IdleService()), pid=701)
                reading = captured_reading(running=(step,))
                with (mock.patch.object(admission_client, "view", return_value=reading),
                      mock.patch.object(memory_feed, "read_ci_runner", return_value=runner)):
                    page = memory_feed.reading()
                row, = [row for row in page["walk"] if row.get("ci")]
                row = cast(memory_feed.RunRow, row)
                if "ci_runners" not in row:
                    self.fail("the CI row has no runner evidence")
                self.assertEqual(row["ci_runners"][0]["state"], "busy" if busy else "idle")
                self.assertEqual(page["ci_ram"], 0.125)
                self.assertEqual(page["taken"], page["used"] + page["ci_ram"] + page["reserved"])
                self.assertEqual(page["taken"], 0.125)
                self.assertEqual(row.get("admitted"), busy)

    def test_the_feed_publishes_configured_colors_for_both_themes(self) -> None:
        original = self.configuration.read_text()
        keys = ("remote_memory", "machine_natedev", "machine_mac", "machine_aws")
        for theme in ("light", "dark"):
            for index, key in enumerate(keys):
                value = f"{index + 1:06x}"
                text = "\n".join(f"{key}_{theme}={value}" if line.startswith(f"{key}_{theme}=") else line
                                 for line in original.splitlines())
                with self.subTest(theme=theme, key=key):
                    _ = self.configuration.write_text(text)
                    configuration = memory_feed.read_configuration()
                    with mock.patch.object(admission_client, "view", return_value=captured_reading()):
                        page = memory_feed.reading(configuration)
                    palette = page["colors"]["light"] if theme == "light" else page["colors"]["dark"]
                    actual = palette["remote_memory"] if key == "remote_memory" else palette["machines"].get(key.removeprefix("machine_"))
                    self.assertEqual(actual, "#" + value)

    def test_waiting_and_unmatched_ci_jobs_do_not_claim_admission(self) -> None:
        from scripts.build_queue.scope_activity import WaitingServiceJob, UnmatchedServiceJob

        for state in (WaitingServiceJob(199_000), UnmatchedServiceJob(199_000), RunningServiceJob(199_000)):
            with self.subTest(state=state):
                step = admission_records.RunningStep("", "hana-ci.slice", "service", "/ci", 0,
                    admission_records.UnownedSliceMemory(admission_records.GIB // 8, owner="hana-ci.slice",
                        job_state=RunningServiceJob(199_000), service_job_state=state, pid=701), pid=701)
                reading = with_wait_reason(captured_reading(running=(step,)), admission_records.StartSpacingWait(110))
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                ci, = [row for row in page["walk"] if row.get("ci")]
                self.assertEqual(ci.get("admitted"), isinstance(state, RunningServiceJob))
                self.assertIn("start spacing", page["waiters"][0]["reason"])
                self.assertEqual(page["taken"], 0.125)
                self.assertEqual(page["ci_ram"], 0.125)

    def test_full_reservations_and_shortfall_reach_the_page(self) -> None:
        reading = captured_reading()
        room = reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        holds = (
            admission_records.RunKeepAside(admission_records.RunMemory("first", 16 * admission_records.GIB,
                admission_records.AnonymousRunUse(4 * admission_records.GIB)), 12 * admission_records.GIB),
            admission_records.RunKeepAside(admission_records.RunMemory("second", 3 * admission_records.GIB,
                admission_records.AnonymousRunUse(8 * admission_records.GIB)), 0),
        )
        changed = replace(reading, explanation=replace(reading.explanation, room=replace(room,
            available=7 * admission_records.GIB, safety=3 * admission_records.GIB, used=12 * admission_records.GIB,
            kept=12 * admission_records.GIB, signed_room=-8 * admission_records.GIB,
            cap=16 * admission_records.GIB, runs=holds)))
        with mock.patch.object(admission_client, "view", return_value=changed):
            page = memory_feed.reading()
        self.assertEqual(page["reserved"], 12)
        self.assertEqual(page["shortfall"], 8)
        self.assertEqual(page["room"], 0)
        self.assertEqual(page["taken"], 24)
        self.assertEqual(sum(cast(memory_feed.RunRow,row)["reserved"] for row in page["walk"] if row.get("queued")), 12)
        self.assertNotIn("shown_reserved", page)
        # Clamping combined need minus combined use would incorrectly return 7 GiB.
        self.assertNotEqual(page["reserved"], (16 + 3) - (4 + 8))

    def test_page_heading_shows_next_step_room_without_a_waiting_count(self) -> None:
        page = self.expected_page()
        self.assertIn('id="answer" data-tip="answer">Next step room</div>', page)
        self.assertNotIn("steps wait", page)
        self.assertNotIn("step waits", page)

    def test_parentless_owner_keeps_its_name_and_uses_the_repository_for_steps(self) -> None:
        reading = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        labels = {step.session_id: unit_labels.UnitLabels("solo")
                  for step in (*reading.view.running, *reading.view.waiting)}
        with (mock.patch.object(admission_client, "view", return_value=reading),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
            page = memory_feed.reading()
        running, = [row for row in page["walk"] if row.get("queued")]
        waiting, = page["waiters"]
        for row in (running, waiting):
            self.assertEqual(row.get("showrunner"), "solo")
            self.assertEqual(row["unit"], "—")
            self.assertTrue(row["name"].startswith("fixture-project "))


    def test_local_test_counters_reach_the_page_through_admission(self) -> None:
        for step in ("targeted-test", "merge-gate", "nextest", "test", "t"):
            with self.subTest(step=step):
                with AdmissionFixture() as fixture, fixture.loop_turn():
                    owner = fixture.proc.spawn()
                    sidecar = fixture.scope(owner)
                    request = replace(fixture.request(owner=owner, step=step), sidecar=str(sidecar))
                    grant = only_admitted(fixture.server.handle_request(request))
                    phase = "suites:1" if step in ("targeted-test", "merge-gate") else "test:1"
                    counter = progress_profile.ProgressCounter(phase, 7, 12)
                    progress_profile.publish_counter(sidecar, counter)
                    _ = fixture.run_pass()
                    reply, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(reply, wire.ViewReply)
                    reading = reply.reading
                    running, = [row for row in reading.view.running if row.pid == owner.pid]
                    self.assertEqual(queue_view.step_counter(running), counter)
                    fixture.release(grant)
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                row, = [row for row in page["walk"] if row["pid"] == owner.pid]
                self.assertEqual(row.get("progress"), {"kind": "RowProgress", "phase": "testing",
                    "unit": "modules" if phase == "suites:1" else "tests",
                    "done": 7, "total": 12, "percent": counter.percent})
                self.assertTrue(row.get("queued"))


    def test_pressure_gauges_use_the_admission_measurements(self) -> None:
        baseline = reservation_reading()
        for pressure in (admission_records.NoPressureHold(admission_records.PressureMeasurements(1.25, 2.5, 62.9)),
                         admission_records.MemoryPressure(5.01, 3, 70, condition="full")):
            with self.subTest(pressure=pressure):
                inputs = replace(baseline.explanation.decided_from, pressure=pressure)
                reading = gate.queue_read_from(inputs, admission_policy_fixture())
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                if isinstance(pressure, admission_records.NoPressureHold):
                    self.assertEqual(page["psi"], {"full":1.25,"some":2.5,"swap":62.9})
                    self.assertEqual(page["pressure"], "none")
                else:
                    self.assertEqual(page["psi"], {"full":5.01,"some":3,"swap":70})
                    self.assertNotEqual(page["pressure"], "none")

    def test_pressure_text_on_the_page_rounds_down_to_one_decimal(self) -> None:
        baseline = reservation_reading()
        for pressure in (admission_records.MemoryPressure(0, swap_used_percent=95.99, condition="swap"),
                         admission_records.MemoryPressure(0, swap_used_percent=95.99)):
            with self.subTest(condition=pressure.condition):
                inputs = replace(baseline.explanation.decided_from, pressure=pressure)
                reading = gate.queue_read_from(inputs, admission_policy_fixture())
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                self.assertIn("swap 95.9% used", page["pressure"])
                self.assertNotIn("96.0%", page["pressure"])


    def test_native_mac_row_shows_stage_unit_and_memory_without_changing_local_totals(self) -> None:
        baseline = reservation_reading()
        now = baseline.explanation.now
        build = queue_view.MacBuild(710001, "/Users/nate/rust/hana", now - 45,
            "cargo nextest run --test-threads 12", "hana", queue_view.MacTesting(710002),
            3 * admission_records.GIB, 8, progress_profile.ProgressCounter("test:1", 7, 10), "trunk")
        native = queue_view.MacBuildReading(now, (build,))
        with mock.patch.object(admission_client, "view", return_value=baseline):
            before = memory_feed.reading()
        with (mock.patch.object(admission_client, "view", return_value=baseline),
              mock.patch.object(queue_view, "read_mac_builds", return_value=native),
              mock.patch.object(memory_feed, "mac_build_units", return_value={710001: unit_labels.UnitLabels("", unit="trunk")})):
            page = memory_feed.reading()
        row = cast(memory_feed.MacBuildRow, next(row for row in page["walk"] if row["pid"] == 710001))
        self.assertEqual(row["kind"], "mac-build")
        self.assertEqual(row["name"], "hana nextest")
        self.assertEqual(row["repository"], "hana")
        self.assertEqual(row["showrunner"], "—")
        self.assertEqual(row["machine"], "mac")
        self.assertEqual(row["unit"], "trunk")
        self.assertEqual(row["stage"], queue_view.MacTesting(710002))
        self.assertEqual(row["used"], 3)
        self.assertEqual(row["vcpus"], 12)
        self.assertEqual(row.get("progress"), {"kind": "RowProgress", "phase": "testing", "unit": "tests",
            "done": 7, "total": 10, "percent": 70})
        for field in ("counted", "reserved", "need"):
            self.assertNotIn(field, row)
        for field in ("used", "reserved", "taken", "room", "cap", "other"):
            self.assertEqual(page.get(field), before.get(field), field)

    def test_ci_runner_records_name_the_owner_in_running_and_waiting(self) -> None:
        base = with_wait_reason(reservation_reading(), admission_records.ReadyForAdmission())
        memory = admission_records.UnownedSliceMemory(admission_records.GIB // 8,
            cgroup=str(self.root / "ci"), owner="hana-ci.slice", pid=701, job_state=IdleService())
        service = admission_records.RunningStep("", "hana-ci.slice", "service", "/ci", 0, memory, pid=701)
        (self.root / "ci" / "github-runner-hana-linux-1.service").mkdir(parents=True)
        (self.root / "ci" / "github-runner-hana-linux-2.service").mkdir()
        inputs = base.explanation.decided_from
        original = inputs.running[0]
        for runner in ("hana-linux-1", "hana-linux-2"):
            with self.subTest(runner=runner):
                running = replace(original, unit=runner, session_id="", session_name="unrelated display name")
                reading = gate.queue_read_from(replace(inputs, running=(running, service)), admission_policy_fixture())
                waiting = replace(base.view.waiting[0], unit=runner, session_id="", session_name="unrelated display name")
                reading = replace(reading, view=replace(reading.view, waiting=(waiting,)))
                def runner_record(path: Path, proc: Path) -> memory_feed.CIRunner:
                    del proc
                    return {"unit": path.name, "name": path.name.removeprefix("github-runner-").removesuffix(".service"),
                            "state": "idle", "used": 0.0625, "pid": 701}
                with (mock.patch.object(admission_client, "view", return_value=reading),
                      mock.patch.object(memory_feed, "read_ci_runner", side_effect=runner_record)):
                    page = memory_feed.reading()
                run = next(row for row in page["walk"] if row.get("queued"))
                self.assertEqual(run.get("showrunner"), "hana CI")
                self.assertEqual(page["waiters"][0]["showrunner"], "hana CI")
                self.assertEqual(run["unit"], runner)
                ci = next(row for row in page["walk"] if row.get("ci"))
                self.assertEqual(ci["unit"], "hana CI")
        impostor = replace(base.view.waiting[0], unit="other", session_name="hana-linux-1")
        self.assertNotEqual(memory_feed.feed_step_labels(impostor, {}, frozenset({"hana-linux-1"})).owner_text, "hana CI")

    def test_mac_unit_requires_a_claim_and_uses_its_staff_record(self) -> None:
        from scripts.copies import copies
        from scripts.mac_test import claim_state, floor_claims

        build = queue_view.MacBuild(100, "/Users/nate/rust/hana", 200_000,
            repository="hana", recorded_unit="verify", session="owner")
        record: claim_state.ProcessBoundClaim = {"claim_id": "claim", "episode": "episode", "attempt": "attempt",
            "submitting_session": "owner", "pid": 100, "proc_start": "start", "what": "test",
            "worktree": "/Users/nate/rust/hana", "since": "2026-11-12T18:00:00+00:00"}
        claim = claim_state.HeldClaim(claim_state.RepositoryScope("hana"), self.root / "claim.json", record)
        lease = floor_claims.HeldMirror(floor_claims.MirrorIdentity("mac", build.folder), "owner")
        identity = unit_labels.UnitLabels("worker", "hana", "trunk")
        for claims, leases in (((claim,), ()), ((), (lease,)), ((), ())):
            with (mock.patch.object(claim_state, "read_claims", return_value=claims),
                  mock.patch.object(copies, "held_remote_mirrors", return_value=leases),
                  mock.patch.object(unit_labels, "unit_labels_for", return_value={"owner": identity})):
                result = memory_feed.mac_build_units((build,))
            self.assertEqual(result.get(100, unit_labels.UnitLabels("")).unit, "trunk" if claims else "—")

    def test_mac_remote_claim_matches_its_session_and_repository(self) -> None:
        from scripts.copies import copies
        from scripts.mac_test import claim_state

        build = queue_view.MacBuild(100, "/Users/nate/.local/state/mac-test/mirror/hana", 200_000,
            repository="hana", recorded_unit="verify", session="claim-session")
        record: claim_state.RemoteJobClaim = {"claim_id": "claim", "episode": "episode", "attempt": "attempt",
            "submitting_session": "claim-session", "pid": 900, "proc_start": "start", "what": "nextest hana",
            "worktree": "/home/nate/source", "since": "2026-11-12T18:00:00+00:00", "remote_host": "mac", "repo_name": "hana"}
        claim = claim_state.HeldClaim(claim_state.RepositoryScope("hana"), self.root / "claim.json", record)
        identity = unit_labels.UnitLabels("worker", "hana", "trunk")
        with (mock.patch.object(claim_state, "read_claims", return_value=(claim,)),
              mock.patch.object(copies, "held_remote_mirrors", return_value=()),
              mock.patch.object(unit_labels, "unit_labels_for", return_value={"claim-session": identity})):
            self.assertEqual(memory_feed.mac_build_units((build,))[100], identity)
            self.assertEqual(memory_feed.mac_build_units((replace(build, repository="cargo-liner"),)), {})
            self.assertEqual(memory_feed.mac_build_units((replace(build, session="unclaimed"),)), {})

    def test_old_native_mac_readings_hide_their_process_rows(self) -> None:
        baseline = reservation_reading()
        now = baseline.explanation.now
        build = queue_view.MacBuild(710001, "/Users/nate/mac-clone", now - 120)
        for read_at, error in ((now - 90, ""), (now - 30, "Mac unreachable"), (0, "Mac reading missing")):
            with self.subTest(read_at=read_at, error=error):
                native = queue_view.MacBuildReading(read_at, (build,), error)
                with (mock.patch.object(admission_client, "view", return_value=baseline),
                      mock.patch.object(queue_view, "read_mac_builds", return_value=native)):
                    page = memory_feed.reading()
                self.assertFalse(any(row["pid"] == 710001 for row in page["walk"]))
                payload = memory_feed.page_json(page)
                self.assertIn("Mac reading is old", payload)
                if error:
                    self.assertNotIn(error, payload)
                self.assertEqual(sum(row.get("kind") == "mac-unavailable" for row in page["walk"]), 1)

    def test_configuration_reads_all_keys_from_the_feed_config(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as read:
            configuration = memory_feed.read_configuration()
        self.assertIn(mock.call("memory_feed"), read.call_args_list)
        self.assertEqual(configuration, memory_feed.FeedConfiguration(self.output, 0.125, 7, 6, 300, configuration.colors))

    def test_configuration_expands_the_output_folder(self) -> None:
        original = self.configuration.read_text()
        _ = self.configuration.write_text(original.replace(f"output_folder={self.output}", "output_folder=~/page"))
        self.assertEqual(memory_feed.read_configuration().output_folder, Path.home() / "page")
        _ = self.configuration.write_text(original.replace(f"output_folder={self.output}", "output_folder=$FEED_TEST_ROOT/page"))
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
        def view() -> admission_records.QueueRead:
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

    def test_page_proof_removes_chrome_files_after_exit_and_a_killed_child(self) -> None:
        _ = write_config_fixture(self.root / "build_folders.conf")
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        chrome = self.root / "chrome"
        report = self.root / "chrome-report.json"
        _ = chrome.write_text(f"#!{sys.executable}\n" + textwrap.dedent(r"""
            import base64, hashlib, json, os, signal, socket, sys, tempfile
            from pathlib import Path
            profile = next(argument.split('=', 1)[1] for argument in sys.argv if argument.startswith('--user-data-dir='))
            allocated = tempfile.mkdtemp(prefix='url_fetcher_')
            record = {'profile': profile, 'temporary': os.environ['TMPDIR'], 'allocated': allocated}
            report = Path(os.environ['CHROME_REPORT'])
            report.write_text(json.dumps(record))
            if os.environ['CHROME_END'] == 'request':
                with socket.socket() as server:
                    server.bind(('127.0.0.1', 0))
                    server.listen(1)
                    print(f'DevTools listening on ws://127.0.0.1:{server.getsockname()[1]}/debugger', file=sys.stderr, flush=True)
                    with server.accept()[0] as connection:
                        stream = connection.makefile('rb')
                        headers = {}
                        while line := stream.readline().strip():
                            if b':' in line:
                                key, value = line.split(b':', 1)
                                headers[key.lower()] = value.strip()
                        key = headers[b'sec-websocket-key'] + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                        accept = base64.b64encode(hashlib.sha1(key).digest())
                        connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                        frame = stream.read(2)
                        size = frame[1] & 127
                        if size == 126:
                            size = int.from_bytes(stream.read(2), 'big')
                        mask = stream.read(4)
                        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(stream.read(size)))
                        record['request'] = json.loads(payload)['method']
                        report.write_text(json.dumps(record))
                        os.kill(os.getpid(), signal.SIGKILL)
            if os.environ['CHROME_END'] == 'kill':
                os.kill(os.getpid(), signal.SIGKILL)
            sys.exit(1)
        """))
        chrome.chmod(0o755)
        for ending in ('exit', 'kill', 'request'):
            with self.subTest(ending=ending), tempfile.TemporaryDirectory(dir=self.root) as directory:
                caller = Path(directory)
                environment = {**os.environ, "TMPDIR": str(caller), "CHROME": str(chrome),
                    "CHROME_REPORT": str(report), "CHROME_END": ending}
                result = subprocess.run([executable, str(Path(memory_feed.__file__).with_name("memory_page_proof.mjs")),
                    str(self.root / "proof")], env=environment, text=True, capture_output=True,
                    timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                self.assertNotEqual(result.returncode, 0)
                if ending == 'request':
                    self.assertRegex(result.stderr, "Chrome (closed during a debugger request|debugger connection closed)")
                else:
                    self.assertIn("Chrome closed before its debugger started", result.stderr)
                record = cast(dict[str, str], json.loads(report.read_text()))
                if ending == 'request':
                    self.assertEqual(record['request'], 'Target.createTarget')
                profile, temporary, allocated = (Path(record[key]) for key in ("profile", "temporary", "allocated"))
                self.assertTrue(temporary.is_relative_to(profile))
                self.assertTrue(allocated.is_relative_to(temporary))
                self.assertFalse(profile.exists())
                self.assertEqual(list(caller.iterdir()), [])

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
        pressure = memory_feed.captured_pressure(admission_records.NoPressureHold(), admission_records.HostMemory(0, 0))
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
            def view() -> admission_records.QueueRead:
                report_calls_per_cycle.append(provider.call_count)
                return captured_reading()

            queue.side_effect = view
            with self.assertRaises(FeedStopped):
                _ = memory_feed.main([])
        self.assertEqual(queue.call_count, 3)
        self.assertEqual(report_calls_per_cycle, [1, 1, 2])
        self.assertEqual(provider.call_args_list, [mock.call(7, n=3), mock.call(7, n=3)])

    def test_mac_readings_follow_the_configured_refresh_window(self) -> None:
        for interval, expected in ((30, [710001, 710001, 710002]), (60, [710001, 710001, 710001])):
            with self.subTest(interval=interval):
                self.configure()
                _ = self.configuration.write_text(self.configuration.read_text().replace(
                    "mac_activity_refresh_seconds=30", f"mac_activity_refresh_seconds={interval}"))
                builds = iter((710001, 710002))
                published: list[int] = []

                def refresh() -> None:
                    pid = next(builds)
                    _ = (self.root / "mac-builds.json").write_text(json.dumps({"read_at": 200_000,
                        "builds": [queue_view.mac_build_record(queue_view.MacBuild(pid, f"/private/mac-{pid}", 199_900))], "error": ""}))

                def after_cycle(seconds: float) -> None:
                    del seconds
                    page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
                    rows = cast(list[dict[str, object]], page["walk"])
                    row, = [row for row in rows if row.get("machine") == "mac"]
                    published.append(cast(int, row["pid"]))
                    if len(published) == 3:
                        raise FeedStopped

                with (mock.patch.object(admission_client, "view", return_value=captured_reading()),
                      mock.patch.object(queue_view, "refresh_mac_builds", side_effect=refresh),
                      mock.patch.object(time, "monotonic", side_effect=[0.0, 29.0, 30.0]),
                      mock.patch.object(time, "sleep", side_effect=after_cycle)):
                    with self.assertRaises(FeedStopped):
                        _ = memory_feed.main([])
                self.assertEqual(published, expected)

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

    def host_swap(self, total: int, used: int) -> admission_records.HostMemory:
        _ = (self.root / "proc/meminfo").write_text(
            f"SwapTotal: {total // 1024} kB\nSwapFree: {(total - used) // 1024} kB\n")
        return admission_records.HostMemory(0, 0, total, total - used)

    def test_swap_accounts_for_the_host_and_names_the_agent_owner_once(self) -> None:
        host = self.host_swap(32 * admission_records.GIB, 20 * admission_records.GIB)
        _ = self.cgroup("user.slice", 16 * admission_records.GIB)
        user = "user.slice/user@1000.service"
        _ = self.cgroup(user, 15 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice", 12 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice/app-agents.slice", 10 * admission_records.GIB)
        agent = self.cgroup(user + "/app.slice/app-agents.slice/first.scope", 4 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice/app-agents.slice/second.scope", 6 * admission_records.GIB)
        _ = self.cgroup(user + "/app.slice/app-browser.scope", 2 * admission_records.GIB)
        _ = self.cgroup(user + "/session.slice", 2 * admission_records.GIB)
        _ = self.cgroup(user + "/builds.slice", admission_records.GIB)
        scope = self.cgroup(user + "/builds.slice/run.scope", admission_records.GIB)
        _ = self.cgroup("system.slice", 2 * admission_records.GIB)
        _ = self.cgroup("hana.slice", admission_records.GIB)
        queued = reservation_reading().view.running[0]
        memory = queued.measured
        assert isinstance(memory, admission_records.MeasuredMemory)
        queued = replace(queued, measured=replace(memory, anonymous_source=admission_records.ScopeSource(scope, frozenset())))
        reads: Counter[Path] = Counter()
        original = Path.read_text

        def read(path: Path) -> str:
            reads[path] += 1
            return original(path)

        names = read_step_names()
        with mock.patch.object(Path, "read_text", new=read):
            swap = memory_feed.read_swap((queued, queued), {}, 6, names, host)
        assert isinstance(swap, memory_feed.SwapReading)
        rows = {row["name"]: row for row in swap.rows}
        self.assertEqual(rows["agent sessions (2)"]["bytes"], 10 * admission_records.GIB)
        self.assertEqual(rows["other apps: browser"]["bytes"], 2 * admission_records.GIB)
        self.assertEqual(rows["desktop session"]["bytes"], 2 * admission_records.GIB)
        self.assertEqual(rows["system services"]["bytes"], 2 * admission_records.GIB)
        self.assertEqual(rows["hana CI"]["bytes"], admission_records.GIB)
        self.assertEqual(rows[queue_lines.step_text(queued, read_step_names())]["bytes"], admission_records.GIB)
        self.assertEqual(rows["not in a group"]["bytes"], admission_records.GIB)
        self.assertEqual(rows["not attributed"]["bytes"], admission_records.GIB)
        self.assertEqual(rows["Total"]["bytes"], swap.used)
        self.assertEqual(sum(row["bytes"] for row in swap.rows if row["kind"] != "total"), swap.used)
        self.assertEqual(swap.files_read, sum(reads.values()))
        self.assertTrue(all(count == 1 for count in reads.values()))
        self.assertNotIn(agent / "memory.swap.current", reads)

    def test_swap_keeps_the_unattributed_bytes_without_rounding_them_away(self) -> None:
        host = self.host_swap(2 ** 20, 2 ** 20)
        _ = self.cgroup("system.slice", 2 ** 20 - 7)
        swap = memory_feed.read_swap((), {}, 6, read_step_names(), host)
        assert isinstance(swap, memory_feed.SwapReading)
        residual = next(row for row in swap.rows if row["kind"] == "unattributed")
        self.assertEqual(residual["bytes"], 7)
        self.assertEqual(sum(row["bytes"] for row in swap.rows[:-1]), swap.used)
        self.assertIn("not attributed: 7 B", memory_feed.swap_text(swap.rows))

    def test_swap_exposes_a_signed_counter_difference_without_changing_owner_values(self) -> None:
        host = self.host_swap(2 ** 20, 2 ** 20)
        _ = self.cgroup("system.slice", 2 ** 20 + 7)
        swap = memory_feed.read_swap((), {}, 6, read_step_names(), host)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap.rows[0]["bytes"], 2 ** 20 + 7)
        residual = next(row for row in swap.rows if row["kind"] == "unattributed")
        self.assertEqual(residual["bytes"], -7)
        self.assertEqual(sum(row["bytes"] for row in swap.rows[:-1]), swap.used)

    def test_swap_names_ci_instead_of_the_raw_service_step(self) -> None:
        host = self.host_swap(2 * admission_records.GIB, admission_records.GIB)
        _ = self.cgroup("hana.slice", admission_records.GIB)
        path = self.cgroup("hana.slice/hana-ci.slice", admission_records.GIB)
        step = admission_records.RunningStep("", "hana-ci.slice", "service", str(path), 0,
            admission_records.UnownedSliceMemory(admission_records.GIB, cgroup=str(path), owner="hana-ci.slice",
                                    pid=admission_records.PidNotRecorded(), job_state=IdleService()), pid=admission_records.PidNotRecorded())
        swap = memory_feed.read_swap((step,), {}, 6, read_step_names(), host)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual(swap.rows[0]["name"], "hana CI")
        self.assertEqual(swap.rows[0]["bytes"], admission_records.GIB)

    def test_swap_names_between_steps_owners_in_the_shared_rows(self) -> None:
        host = self.host_swap(4 * admission_records.GIB, 2 * admission_records.GIB)
        _ = self.cgroup("builds.slice", 2 * admission_records.GIB)
        steps: list[admission_records.RunningStep] = []
        labels = {name: unit_labels.UnitLabels(name) for name in ("owner-a", "owner-b")}
        for owner in labels:
            path = self.cgroup("builds.slice/" + owner + ".scope", admission_records.GIB)
            steps.append(admission_records.RunningStep(owner, owner, "between steps", str(path), 0,
                admission_records.UnownedSliceMemory(admission_records.GIB, cgroup=str(path), owner=owner, pid=admission_records.PidNotRecorded()),
                pid=admission_records.PidNotRecorded()))
        swap = memory_feed.read_swap(steps, labels, 6, read_step_names(), host)
        assert isinstance(swap, memory_feed.SwapReading)
        self.assertEqual({row["name"] for row in swap.rows if row["kind"] == "owner"}, set(labels))
        self.assertTrue(all("between steps" in row["detail"] for row in swap.rows if row["kind"] == "owner"))

    def test_swap_small_groups_and_total_use_the_captured_host_reading(self) -> None:
        host = self.host_swap(4 * admission_records.GIB, 3 * admission_records.GIB)
        for number in range(3):
            _ = self.cgroup(f"owner-{number}.slice", admission_records.GIB // 2)
        reading = captured_reading()
        captured_host = reading.explanation.decided_from.host
        assert isinstance(captured_host, admission_records.HostMemory)
        reading = gate.queue_read_from(replace(reading.explanation.decided_from, host=replace(captured_host,
            swap_total=host.swap_total, swap_free=host.swap_free)), admission_policy_fixture())
        # A newer host file must not change the captured pressure display.
        _ = (self.root / "proc/meminfo").write_text("SwapTotal: 8388608 kB\nSwapFree: 8388608 kB\n")
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading(replace(memory_feed.read_configuration(), swap_rows=1))
        self.assertEqual(page["swap_used"], 3)
        self.assertEqual(page["swap_total"], 4)
        self.assertEqual([row["kind"] for row in page["swap_by"]], ["owner", "smaller", "unattributed", "total"])
        self.assertEqual(sum(row["bytes"] for row in page["swap_by"][:-1]), 3 * admission_records.GIB)
        self.assertEqual(page["swap_by"][1]["bytes"], admission_records.GIB)

    def test_between_steps_holds_name_their_distinct_owners(self) -> None:
        reading = captured_reading()
        room = reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        holds = tuple(admission_records.RunKeepAside(admission_records.RunMemory(owner, admission_records.GIB, admission_records.AnonymousRunUse(0)), admission_records.GIB)
                      for owner in ("owner-a", "owner-b", "owner-c"))
        reading = replace(reading, explanation=replace(reading.explanation, room=replace(room, runs=holds)))
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading()
        self.assertEqual({row["name"] for row in page["walk"]}, {"owner-a", "owner-b", "owner-c"})
        self.assertTrue(all(row.get("detail") == "between steps" for row in page["walk"]))

    def test_pressure_failure_names_the_path_without_the_python_error(self) -> None:
        values = memory_feed.captured_pressure(admission_records.PressureUnavailable(
            f"Pressure is unreadable: {self.root / 'proc/pressure/memory'}"), admission_records.HostMemory(0, 0))
        for value in (values["some"], values["full"]):
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

    def test_installed_feed_sources_and_startup_files_change_the_fixed_version(self) -> None:
        installed = self.root / "installed"
        for relative in memory_feed.FEED_CODE_PATHS:
            target = installed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text("fixture\n")
        cases = (
            ("scripts/build_queue/memory_feed.py", True),
            ("scripts/buildlog/charts.py", True),
            ("scripts/buildlog/holds.py", True),
            ("scripts/build_queue/memory_live.html", True),
            ("config/memory_feed.conf", True),
            ("config/build_steps.conf", False),
            ("scripts/unrelated.py", False),
        )
        before = memory_feed.installed_feed_code_version(installed)
        for relative, changes_version in cases:
            with self.subTest(path=relative):
                target = installed / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                previous = target.read_text() if target.exists() else ""
                _ = target.write_text("changed fixture\n")
                self.assertEqual(memory_feed.installed_feed_code_version(installed) != before, changes_version)
                _ = target.write_text(previous)
        (installed / "scripts/build_queue/memory_feed.py").unlink()
        self.assertEqual(memory_feed.installed_feed_code_version(installed), "")

    def test_an_installed_schema_change_requires_a_restart_before_the_next_read(self) -> None:
        installed = self.root / "installed"
        source = installed / "scripts" / "build_queue" / "admission_records.py"
        for relative in memory_feed.FEED_CODE_PATHS:
            target = installed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            _ = target.write_text("fixture\n")
        _ = source.write_text("schema = 'before'\n")
        before = memory_feed.installed_feed_code_version(installed)

        def install_schema(seconds: float) -> None:
            self.assertEqual(seconds, 0.125)
            _ = source.write_text("schema = 'after'\n")
            self.assertNotEqual(memory_feed.installed_feed_code_version(installed), before)

        with (mock.patch.object(admission_client, "view", return_value=captured_reading()) as view,
              mock.patch.object(memory_feed, "LOADED_FEED_CODE_VERSION", before),
              mock.patch.object(time, "sleep", side_effect=install_schema)):
            self.assertEqual(memory_feed.main(["--code-root", str(installed)]), 1)
        view.assert_called_once_with()
        self.assertNotEqual(memory_feed.installed_feed_code_version(installed), before)
        changed = replace(captured_reading(), explanation=replace(captured_reading().explanation, now=200_001))
        with (mock.patch.object(admission_client, "view", return_value=changed) as restarted,
              mock.patch.object(memory_feed, "LOADED_FEED_CODE_VERSION", memory_feed.installed_feed_code_version(installed))):
            self.assertEqual(memory_feed.main(["--once", "--code-root", str(installed)]), 0)
        restarted.assert_called_once_with()
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertEqual(page["read_at"], 200_001)

    def test_each_wait_state_keeps_its_reason_and_the_other_readings(self) -> None:
        reasons = (
            (admission_records.PressureUnavailable("cannot read /private/proc/pressure/memory"), "/private/proc/pressure/memory"),
            (admission_records.StartSpacingWait(200_010), "start spacing"),
            (admission_records.BehindHead("first", 1, "head-session"), "behind"),
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
        cases = (("scripts.build_queue", "snapshot_codec", "codec"), ("scripts.buildlog", "charts", "charts"))
        for package, module, alias in cases:
            with self.subTest(module=module):
                installed = self.root / "installed" / module
                source = Path(memory_feed.__file__).resolve().parents[1]
                _ = shutil.copytree(source, installed / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                (installed / "config").mkdir()
                _ = shutil.copyfile(conf.path("memory_feed"), installed / "config/memory_feed.conf")
                probe = textwrap.dedent("""
                    import builtins
                    import json
                    import sys
                    from pathlib import Path
                    from unittest import mock

                    root = Path(sys.argv[1])
                    source = root / sys.argv[2].replace('.', '/') / (sys.argv[3] + '.py')
                    original_import = builtins.__import__
                    changed = False
                    def install_during_import(name, globals=None, locals=None, fromlist=(), level=0):
                        global changed
                        result = original_import(name, globals, locals, fromlist, level)
                        if name == sys.argv[2] and sys.argv[3] in fromlist and not changed:
                            changed = True
                            source.write_text(source.read_text() + '\\nfrom dataclasses import dataclass\\n'
                                '@dataclass(frozen=True)\\nclass InstalledSchemaRevision:\\n    revision: str\\n')
                        return result
                    builtins.__import__ = install_during_import
                    from scripts.build_queue import memory_feed
                    builtins.__import__ = original_import
                    assert changed
                    assert not hasattr(getattr(memory_feed, sys.argv[4]), 'InstalledSchemaRevision')
                    with mock.patch.object(memory_feed.admission_client, 'view',
                            side_effect=AssertionError('The old decoder must not read the queue')):
                        status = memory_feed.main(['--once', '--code-root', str(root)])
                    print(json.dumps({'status': status, 'changed':
                        memory_feed.LOADED_FEED_CODE_VERSION != memory_feed.installed_feed_code_version(root)}))
                    sys.exit(status)
                """)
                result = subprocess.run([sys.executable, "-c", probe, str(installed), package, module, alias], cwd=installed,
                    env={**os.environ, "PYTHONPATH": str(installed)}, capture_output=True, text=True,
                    timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), {"status": 1, "changed": True})
                restarted = textwrap.dedent("""
                    import sys
                    from unittest import mock
                    from scripts.build_queue import memory_feed
                    decoded = memory_feed.codec.parse_snapshot_value(
                        {'kind': 'InstalledSchemaRevision', 'revision': 'after'},
                        getattr(memory_feed, sys.argv[2]).InstalledSchemaRevision)
                    with (mock.patch.object(memory_feed.queue_view, 'refresh_mac_builds'),
                          mock.patch.object(memory_feed, 'report_reading',
                            return_value=memory_feed.charts.IndexUnavailable('private report fixture')),
                          mock.patch.object(memory_feed, 'reading', return_value={
                            'ok': True, 'read_at': 200001, 'decoder_revision': decoded.revision})):
                        sys.exit(memory_feed.main(['--once', '--code-root', sys.argv[1]]))
                """)
                result = subprocess.run([sys.executable, "-c", restarted, str(installed), alias], cwd=installed,
                    env={**os.environ, "PYTHONPATH": str(installed)}, capture_output=True, text=True,
                    timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
                self.assertEqual(page["decoder_revision"], "after")

    def test_queue_priority_reaches_the_feed_and_clears_when_removed(self) -> None:
        base = captured_reading()
        first = with_queue_priority(base)
        priority = first.explanation.priority
        assert isinstance(priority, admission_records.QueuePriority)
        two = replace(first, explanation=replace(first.explanation, priority=admission_records.QueuePriority(
            (*priority.entries, {"step": admission_records.SelectedPriorityStep(1, "1", 0), "unit": "cables", "why": "urgent", "set_at": first.explanation.now,
                                 "set_by": "actor"}))))
        cases = ((first, "queue priority: research (the user's bump)"),
                 (two, "queue priority: research (the user's bump), cables (urgent)"), (base, ""))
        for reading, expected in cases:
            with self.subTest(expected=expected):
                with mock.patch.object(admission_client, "view", return_value=reading):
                    payload = memory_feed.reading()
                self.assertEqual([entry["unit"] for entry in payload["unmatched_priorities"]],
                                 ["research", "cables"] if reading is two else ["research"] if reading is first else [])

    def selected_step_reading(self, *, running: bool) -> admission_records.QueueRead:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            if not running:
                fixture.free(0)
                fixture.hold_starts()
            for index in range(3):
                request = fixture.request(since=fixture.clock.now() - 30 + index)
                request = replace(request, identity=replace(request.identity, unit="research"),
                                  sidecar=str(fixture.scope(request.owner)))
                if running:
                    fixture.allow_next_start()
                    _ = only_admitted(fixture.server.handle_request(request))
                else:
                    _ = fixture.server.handle_request(request)
            _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            reading = reply.reading
            rows = ([row["queue_waiter"] for row in reading.explanation.reservations if "queue_waiter" in row]
                    if running else [row for _, row in reading.ordered])
            priority = admission_records.QueuePriority(tuple(
                {"step": admission_records.priority_step(row), "unit": "research", "why": reason,
                 "set_at": fixture.clock.now(), "set_by": "fixture"}
                for row, reason in zip(rows[:2], ("first reason", "second reason"), strict=True)))
            return replace(reading, explanation=replace(reading.explanation, priority=priority))

    def test_priority_reasons_follow_two_selected_steps_of_the_same_unit(self) -> None:
        for running in (False, True):
            with self.subTest(running=running):
                reading = self.selected_step_reading(running=running)
                priority = reading.explanation.priority
                assert isinstance(priority, admission_records.QueuePriority)
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                rows = page["walk"] if running else page["waiters"]
                for entry in priority.entries:
                    row, = [row for row in rows if row["pid"] == entry["step"].pid]
                    self.assertEqual(row.get("priority_text", ""),
                                     f"queue priority: research ({entry['why']})")

    def test_unselected_step_of_a_priority_unit_has_no_priority_note(self) -> None:
        for running in (False, True):
            with self.subTest(running=running):
                reading = self.selected_step_reading(running=running)
                priority = reading.explanation.priority
                assert isinstance(priority, admission_records.QueuePriority)
                selected = {entry["step"].pid for entry in priority.entries}
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                rows = page["walk"] if running else page["waiters"]
                unselected, = [row for row in rows if row["unit"] == "research" and row["pid"] not in selected]
                self.assertEqual(unselected.get("priority_text", ""), "")

    def test_queued_priorities_follow_the_captured_order_and_clear_when_removed(self) -> None:
        base = reservation_reading()
        for units, expected in (((), []), (("research",), ["research"]),
                                (("research", "cables"), ["research", "cables"]),
                                (("cables", "research", "absent"), ["research", "cables"]),
                                (("absent",), [])):
            with self.subTest(priority=units):
                reading = queued_reading(base, units)
                with mock.patch.object(admission_client, "view", return_value=reading):
                    payload = memory_feed.reading()
                self.assertEqual(payload["queued_priority_units"], expected)
                self.assertEqual([row["pid"] for row in payload["waiters"]], [200, 201, 202, 203])
                self.assertEqual([row["since"] for row in payload["waiters"]], [100, 90, 80, 70])
                decoded = cast(dict[str, object], json.loads(memory_feed.page_json(payload)))
                self.assertEqual(decoded["queued_priority_units"], expected)

    def test_queue_notes_follow_the_head_and_the_priority_unit(self) -> None:
        reading = cloud_reading(with_queue_priority(with_wait_reason(
            reservation_reading(), admission_records.StartSpacingWait(110))))
        for priority, unit in ((True, "research"), (False, "research"), (True, "other")):
            with self.subTest(priority=priority, unit=unit):
                labels = {step.session_id: unit_labels.UnitLabels("fixture", "fixture-production", unit)
                          for step in (*reading.view.running, *reading.view.waiting)}
                current = reading if priority else replace(reading, explanation=replace(
                    reading.explanation, priority=admission_records.NoQueuePriority()))
                with (mock.patch.object(admission_client, "view", return_value=current),
                      mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
                    page = memory_feed.reading()
                for row in (*page["walk"], *page["waiters"]):
                    expected = "queue priority: research (the user's bump)" if priority and row["unit"] == "research" else ""
                    self.assertEqual(row.get("priority", {}).get("why", ""), "the user's bump" if expected else "")
                self.assertEqual([entry["unit"] for entry in page["unmatched_priorities"]],
                                 ["research"] if priority and unit == "other" else [])
                heads = [row for row in page["waiters"] if row.get("head")]
                self.assertEqual(len(heads), 1)
                self.assertEqual(page["cloud_slots_used"], 1)
                self.assertEqual(page["cloud_slots"], 1)

    def test_queue_head_and_priority_fields_follow_their_rows(self) -> None:
        reading = cloud_reading(with_queue_priority(with_wait_reason(
            reservation_reading(), admission_records.StartSpacingWait(110))))
        for priority, unit in ((True, "research"), (False, "research"), (True, "other")):
            with self.subTest(priority=priority, unit=unit):
                labels = {step.session_id: unit_labels.UnitLabels("fixture", "fixture-production", unit)
                          for step in (*reading.view.running, *reading.view.waiting)}
                current = reading if priority else replace(reading, explanation=replace(
                    reading.explanation, priority=admission_records.NoQueuePriority()))
                with (mock.patch.object(admission_client, "view", return_value=current),
                      mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
                    page = memory_feed.reading()
                for row in (*page["walk"], *page["waiters"]):
                    expected = "queue priority: research (the user's bump)" if priority and row["unit"] == "research" else ""
                    self.assertEqual(row.get("priority", {}).get("why", ""), "the user's bump" if expected else "")
                self.assertEqual([entry["unit"] for entry in page["unmatched_priorities"]],
                                 ["research"] if priority and unit == "other" else [])
                notes = [row["pid"] for row in page["waiters"] if row.get("head")]
                head = current.explanation.head
                head_pid = (head.waiter.run.pid if isinstance(head.waiter.run, LiveBuildRun) else head.waiter.pid
                            ) if isinstance(head, admission_records.HeadExplanation) else page["waiters"][0]["pid"]
                self.assertEqual(notes, [head_pid])
                self.assertEqual((page["cloud_slots_used"], page["cloud_slots"]), (1, 1))

    def test_mac_idle_alert_reaches_the_feed_and_clears_with_the_watch_state(self) -> None:
        _ = write_idle_config(self.root)
        reading = captured_reading()
        now = reading.explanation.now
        cases = (
            ("idle", 0, "MAC IDLE 10 min: 7 steps wait for the Mac. Blocked by research since "
                + "07:23 (P15 F11 L0 run); /build:mac unblock clears it."),
            ("busy", 0, ""),
            ("unreachable", 0, "MAC UNREACHABLE until 07:38: 7 steps wait for the Mac."),
            ("unreadable", 0, "MAC IDLE WATCH: Mac state unreadable: P15 F11 L0 run."),
            ("idle", 301, "MAC IDLE WATCH: no reading since 07:28; check buildlog-sample.timer."),
            ("absent", 0, ""),
        )
        for state, written_age, expected in cases:
            with self.subTest(state=state, written_age=written_age):
                path = write_idle_alert(self.root / "mac-idle", now, written_age=written_age)
                if state == "absent":
                    path.unlink()
                else:
                    saved = cast(dict[str, object], json.loads(path.read_text()))
                    saved.update(state=state, until=now + 300)
                    _ = path.write_text(json.dumps(saved))
                with mock.patch.object(admission_client, "view", return_value=reading), \
                     mock.patch.object(time, "time", return_value=now):
                    page = memory_feed.reading()
                payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
                self.assertEqual(payload["mac_idle_alert"], expected)

    def test_cloud_rows_show_stage_and_location(self) -> None:
        reading = cloud_reading(reservation_reading())
        first = reading.cloud[0]
        assert isinstance(first.progress, cloud_progress.CloudProgress)
        for stage, region, zone, detail in (
            ("tools ready", "", "", "tools ready"),
            ("tests start", "eu-south-2", "eu-south-2a", "tests start · eu-south-2, zone a"),
            ("", "eu-south-2", "us-east-1b", "eu-south-2, zone us-east-1b"),
            ("", "", "eu-south-2a", "zone eu-south-2a"),
            ("", "eu-south-2", "", "eu-south-2"),
            ("", "", "", ""),
        ):
            with self.subTest(stage=stage, region=region, zone=zone):
                current = replace(first, progress=replace(first.progress, stage=stage, region=region, zone=zone))
                with mock.patch.object(admission_client, "view", return_value=replace(
                        reading, cloud=(current, *reading.cloud[1:]))):
                    page = memory_feed.reading()
                payload = cast(dict[str, object], json.loads(memory_feed.page_json(page)))
                rows = cast(list[dict[str, object]], payload["walk"])
                remote, = [row for row in rows if row.get("kind") == "cloud"]
                expected = " · ".join(filter(None, (detail, "7 of 12 modules", "1 failed module")))
                self.assertEqual(remote["status"], expected)

    def test_cloud_rows_share_the_feed_without_memory_reservations(self) -> None:
        _ = (self.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=5\n")
        reading = cloud_reading(reservation_reading())
        first = reading.cloud[0]
        assert isinstance(first.progress, cloud_progress.CloudProgress)
        states: tuple[tuple[cloud_progress.CloudState, float, str], ...] = (
            ("requesting", first.progress.updated_at, "2 min"),
            ("setup", first.progress.updated_at, "2 min"),
            ("running", first.progress.updated_at, "2 min"),
            ("uploading", first.progress.updated_at, "2 min"),
            ("running", 0.0, "1 min"),
            ("running", reading.explanation.now, ""),
            ("running", reading.explanation.now - 45, "45 s"),
            ("running", reading.explanation.now - 10800, "3 h"),
            ("running", reading.explanation.now - 172800, "2 d"),
        )
        cases: list[tuple[wire.CloudRow, str, str]] = [
            (replace(first, progress=replace(first.progress, state=state, updated_at=updated_at)), state, late_age)
            for state, updated_at, late_age in states]
        cases.extend((
            (replace(first, progress=cloud_progress.NoCloudProgress("missing")), "progress missing", ""),
            (replace(first, progress=cloud_progress.NoCloudProgress("unreadable")), "progress unreadable", ""),
            (replace(first, counter=progress_profile.NoCounter("no cloud counter")), "running", "2 min"),
        ))
        for cloud, state, late_age in cases:
            with self.subTest(state=state, progress=cloud.progress, counter=cloud.counter):
                current = replace(reading, cloud=(cloud, *reading.cloud[1:]))
                with (mock.patch.object(admission_client, "view", return_value=current),
                      mock.patch.object(os, "cpu_count", return_value=17)):
                    page = memory_feed.reading()
                text = memory_feed.page_json(page)
                payload = cast(dict[str, object], json.loads(text))
                self.assertNotIn("cloud", payload)
                self.assertNotIn("cloud_heading", payload)
                self.assertEqual(page["cloud_slots_used"], 1)
                self.assertEqual(page["cloud_slots"], 1)
                self.assertEqual((page["cloud_slots_used"], page["cloud_slots"]), (1, 1))
                runs = cast(list[dict[str, object]], payload["walk"])
                waits = cast(list[dict[str, object]], payload["waiters"])
                remote, = [row for row in runs if row.get("kind") == "cloud"]
                local, = [row for row in runs if row.get("kind") != "cloud"]
                waiter, = [row for row in waits if row.get("kind") == "cloud"]
                self.assertEqual(remote["machine"], "aws")
                self.assertEqual(remote["vcpus"], 32 if isinstance(cloud.progress, cloud_progress.CloudProgress)
                                 else {"kind": "UnknownCpuCount"})
                self.assertEqual(remote["state"], state)
                self.assertEqual(remote["pid"], 700001)
                self.assertEqual(remote["started_at"], first.started_at)
                if isinstance(cloud.counter, progress_profile.ProgressCounter):
                    self.assertEqual(remote["progress"], {"kind": "RowProgress", "phase": "testing",
                        "unit": "modules", "done": 7, "total": 12, "percent": 100 * 7 / 12})
                    self.assertIn("7 of 12 modules", str(remote["status"]))
                else:
                    self.assertEqual(remote["progress"], {"kind": "NoProgress", "reason": cloud.counter.reason})
                    self.assertNotIn("modules unknown", str(remote["status"]))
                if state == "running":
                    self.assertNotIn("running", str(remote["status"]))
                else:
                    self.assertTrue(str(remote["status"]).startswith(state), remote["status"])
                if isinstance(cloud.progress, cloud_progress.CloudProgress):
                    self.assertIn("1 failed module", str(remote["status"]))
                self.assertNotIn("no update for", str(remote["status"]))
                self.assertEqual(remote["late_age"], late_age)
                self.assertEqual(remote["stale"], bool(late_age))
                self.assertEqual(local["machine"], "fixture")
                self.assertEqual(local["vcpus"], 17)
                self.assertGreater(float(str(local["reserved"])), 0)
                self.assertEqual(waiter["pid"], 700002)
                self.assertEqual(waiter["machine"], "")
                self.assertEqual(waiter["late_age"], "")
                self.assertFalse(waiter["stale"])
                self.assertEqual(waiter["since"], reading.cloud[1].since)
                self.assertEqual(waiter["progress"], {"kind": "NoProgress", "reason": "no current counter"})
                self.assertEqual(waiter["status"], waiter["reason"])
                self.assertIn("waits for a cloud slot: 1 of 1 in use", str(waiter["reason"]))
                for row in (remote, waiter):
                    for field in ("used", "counted", "reserved", "need"):
                        self.assertNotIn(field, row)

    def test_project_names_follow_the_queue_formatter_for_running_and_waiting_steps(self) -> None:
        for raw_step, project, folder, expected in (
            ("nextest", "known-project", "/fixture", "known-project nextest"),
            ("nextest", "", "/fixture", "nextest"),
            ("nextest", "", "/absolute/repository", "nextest"),
            ("merge-gate", "", "/fixture", "merge gate"),
        ):
            with self.subTest(step=raw_step, project=project, folder=folder):
                reading = reservation_reading(project=project)
                step = replace(reading.view.running[0], step=raw_step, folder=folder)
                explanation_step = replace(reading.explanation.running[0], step=step)
                reading = replace(reading, view=replace(reading.view, running=(step,)),
                    explanation=replace(reading.explanation, running=(explanation_step,)))
                reading = with_wait_reason(reading, admission_records.StartSpacingWait(110))
                waiter = replace(reading.view.waiting[0], step=raw_step, project=project, folder=folder)
                reading = replace(reading, view=replace(reading.view, waiting=(waiter,)))
                with (mock.patch.object(admission_client, "view", return_value=reading),
                      mock.patch.object(memory_feed, "read_step_names", wraps=read_step_names) as names_read):
                    page = memory_feed.reading()
                names_read.assert_called_once_with()
                self.assertEqual(page["walk"][0]["name"],
                    queue_lines.step_text(reading.view.running[0], read_step_names()))
                self.assertEqual(page["waiters"][0]["name"],
                    queue_lines.step_text(waiter, read_step_names()))
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

    def test_detached_python_counts_in_other_and_reserved_cargo_counts_once(self) -> None:
        for cargo_use in (0, 2 * admission_records.GIB):
            with self.subTest(cargo_use=cargo_use), AdmissionFixture(root=self.root / str(cargo_use)) as fixture:
                python = fixture.fixture_owner(700001, "100")
                process = fixture.proc.path / str(python.pid)
                _ = process.joinpath("comm").write_text("python3\n")
                _ = process.joinpath("cmdline").write_bytes(b"python3\0-m\0unittest\0")
                python_use = 4 * admission_records.GIB
                builds = Path(os.environ["BUILDLOG_BUILDS_CGROUP"])
                detached = builds / "builds-detached.slice"
                write_scope(builds, current=python_use + cargo_use, anonymous=python_use + cargo_use)
                write_scope(detached, current=python_use + cargo_use, anonymous=python_use + cargo_use)
                write_scope(detached / "python.scope", members=(python.pid,),
                    current=python_use + cargo_use, anonymous=python_use + cargo_use)
                if cargo_use:
                    cargo = fixture.fixture_owner(700002, "200")
                    sidecar = fixture.scope(cargo, folder="builds.slice/builds-detached.slice/python.scope/cargo.scope",
                        current=cargo_use, anonymous=cargo_use)
                    request = replace(fixture.request(owner=cargo, need=8 * admission_records.GIB), sidecar=str(sidecar))
                    grant = only_admitted(fixture.server.handle_request(request))
                    self.assertEqual(len(fixture.server.reservations), 1)
                    self.assertIn(grant.reservation, [record.reservation_id for record in fixture.server.reservations.values()])
                fixture.free(60 * admission_records.GIB - python_use - cargo_use)
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                reading = reply.reading
                with mock.patch.object(admission_client, "view", return_value=reading):
                    page = memory_feed.reading()
                self.assertEqual(page["used"], cargo_use / admission_records.GIB)
                self.assertEqual(page["other"], python_use / admission_records.GIB)
                build_rows = [row for row in page["walk"] if not row.get("ci", False)]
                self.assertEqual(len(build_rows), int(bool(cargo_use)))
                self.assertEqual(page["leftovers"], [])
                self.assertTrue(all(row.get("queued", False) for row in build_rows))
                room = reading.explanation.room
                assert isinstance(room, admission_records.MemoryRoom)
                self.assertEqual(len(room.runs), int(bool(cargo_use)))
                self.assertEqual(room.used, cargo_use)
                self.assertEqual(room.used + room.kept, 8 * admission_records.GIB if cargo_use else 0)

    def test_registered_and_unregistered_ci_jobs_count_once_without_margin_text(self) -> None:
        for registered, registered_use, unregistered_use, kept in (
                ((), 0, 6, 0), ((700001,), 2, 4, 6), ((700001, 700002), 6, 0, 10)):
            with self.subTest(registered=registered), AdmissionFixture() as fixture:
                owners: list[wire.Owner] = []
                for pid, worker, used in ((700001, 700011, 2), (700002, 700012, 4)):
                    owner = fixture.fixture_owner(pid, str(pid))
                    owners.append(owner)
                    write_process(fixture.proc.path, worker)
                    _ = (fixture.proc.path / str(worker) / "comm").write_text("Runner.Worker\n")
                    write_process(fixture.proc.path, pid, used * admission_records.GIB, parent=worker)
                    _ = (fixture.proc.path / str(pid) / "comm").write_text("cargo\n")
                ci = Path(os.environ["BUILDLOG_CI_CGROUP"])
                write_scope(ci, members=(700011, 700001, 700012, 700002),
                    current=6 * admission_records.GIB, anonymous=6 * admission_records.GIB)
                for owner in owners:
                    if owner.pid in registered:
                        fixture.allow_next_start()
                        _ = only_admitted(fixture.server.handle_request(fixture.request(owner=owner)))
                fixture.free(20 * admission_records.GIB)
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                with mock.patch.object(admission_client, "view", return_value=reply.reading):
                    page = memory_feed.reading()
                self.assertEqual(page["used"], registered_use)
                self.assertEqual(page["ci_ram"], unregistered_use)
                self.assertEqual(page["cap"], 23)
                self.assertEqual(page["reserved"], kept)
                self.assertEqual(page["room"], 17 - kept)
                self.assertEqual(len(page["walk"]), len(registered) + 1)
                self.assertEqual(sum(row.get("queued", False) for row in page["walk"]), len(registered))
                text = memory_feed.page_json(page)
                self.assertNotIn("CI builds do not wait in the queue yet.", text)
                self.assertNotIn("CI keep-aside", text)

    def test_the_leftover_list_uses_the_queue_rule_and_keeps_all_scope_members(self) -> None:
        ended = leftover_step(45)
        memory = ended.measured
        assert isinstance(memory, admission_records.UnownedSliceMemory)
        activity = memory.activity
        assert isinstance(activity, ScopeProcesses)
        ended = replace(ended, measured=replace(memory, activity=replace(activity, count=2,
            members=(ScopeProcess(45, "python3"), ScopeProcess(46, "helper")))))
        live = replace(ended, pid=60, measured=replace(memory,
            activity=replace(activity, run=LiveBuildRun(60, "live", 199_000))))
        compiler = replace(ended, pid=70, measured=admission_records.CompilerServiceMemory(admission_records.GIB, pid=70))
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
        assert isinstance(room, admission_records.MemoryRoom)
        missing_run = replace(room.runs[0].memory, use=admission_records.RunUseUnavailable("private memory.stat is missing"))
        for cap in (admission_records.CapUnavailable(()), admission_records.CapUnavailable((missing_run,))):
            with self.subTest(cap=cap):
                changed = replace(reading, explanation=replace(reading.explanation, room=replace(room, cap=cap)))
                with mock.patch.object(admission_client, "view", return_value=changed):
                    page = memory_feed.reading()
                self.assertIsInstance(page["cap"], admission_records.CapUnavailable)
                self.assertEqual(page["room"], room.room / admission_records.GIB)
                self.assertTrue(page["walk"])
                if cap.runs:
                    missing = cap.runs[0].use
                    assert isinstance(missing, admission_records.RunUseUnavailable)
                    self.assertEqual(missing.reason, "private memory.stat is missing")

    def test_an_unavailable_run_use_keeps_its_named_reason(self) -> None:
        reading = reservation_reading()
        room = reading.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        missing = admission_records.RunUseUnavailable("private memory.stat is missing")
        run = replace(room.runs[0], memory=replace(room.runs[0].memory, use=missing))
        room = replace(room, runs=(run,), cap=admission_records.CapUnavailable((run.memory,)))
        reading = replace(reading, explanation=replace(reading.explanation, room=room))
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading()
        run_row = page["walk"][0]
        assert "used" in run_row
        self.assertEqual(run_row["used"], missing)
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
        payload["cap"] = admission_records.CapUnavailable(())
        page = cast(dict[str, object], json.loads(memory_feed.page_json(payload)))
        self.assertEqual(page["cap"], {"kind": "CapUnavailable", "runs": []})
        self.assertIn("PressureNotRecorded", json.dumps(page["psi"]))


def write_browser_fixture(folder: Path) -> None:
    """Publish private feed readings for the browser proof."""
    fixture = MemoryFeedTests()
    fixture.setUp()
    _ = cast(object, fixture.enterContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"})))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        configuration = replace(memory_feed.read_configuration(), output_folder=folder)
        memory_feed.publish_page(configuration)
        base = reservation_reading()
        inputs = base.explanation.decided_from
        live = leftover_step(30)
        memory = live.measured
        assert isinstance(memory, admission_records.UnownedSliceMemory)
        activity = memory.activity
        assert isinstance(activity, ScopeProcesses)
        live = replace(live, pid=70, measured=replace(memory,
            activity=replace(activity, run=LiveBuildRun(70, "live", 199_000))))
        compiler = replace(live, pid=80, measured=admission_records.CompilerServiceMemory(admission_records.GIB, pid=80))
        inputs = replace(inputs, now=200_000, pressure=admission_records.PressureUnavailable("Pressure is unreadable: /private/proc/pressure/memory"), running=(*inputs.running, leftover_step(45,
            members=(ScopeProcess(45, "python3"), ScopeProcess(46, "helper"))),
            leftover_step(19 * 60, small=True), leftover_step(2 * 24 * 60, small=True), live, compiler))
        base = gate.queue_read_from(inputs, admission_policy_fixture())
        readings: dict[str, admission_records.QueueRead] = {"good": base}
        for name, reason in (
            ("pressure", admission_records.PressureUnavailable("cannot read /private/proc/pressure/memory")),
            ("spacing", admission_records.StartSpacingWait(110)),
            ("head", admission_records.BehindHead("first", 1, "head-session")),
        ):
            readings[name] = with_wait_reason(base, reason)
        _ = (fixture.root / "cloud_gate.conf").write_text(
            "[cloud_gate]\n# range: 5e-324..\nlog_push_s=5\n")
        cloud = cloud_reading(with_wait_reason(base, admission_records.StartSpacingWait(110)))
        readings["cloud"] = cloud
        first = cloud.cloud[0]
        assert isinstance(first.progress, cloud_progress.CloudProgress)
        for name, stage, region, zone in (
            ("cloud_stage", "tools ready", "", ""),
            ("cloud_region", "tests start", "eu-south-2", "eu-south-2a"),
            ("cloud_zone", "", "eu-south-2", "us-east-1b"),
            ("cloud_empty", "", "", ""),
        ):
            readings[name] = replace(cloud, cloud=(replace(first, progress=replace(
                first.progress, stage=stage, region=region, zone=zone)), *cloud.cloud[1:]))
        readings["cloud_zero"] = replace(cloud, cloud=(replace(first,
            progress=replace(first.progress, updated_at=0.0, failed_modules=123456),
            counter=progress_profile.ProgressCounter("modules", 123456, 999999)), *cloud.cloud[1:]))
        room = base.explanation.room
        assert isinstance(room, admission_records.MemoryRoom)
        missing = replace(room.runs[0].memory, use=admission_records.RunUseUnavailable("private memory.stat is missing"))
        for name, cap in (("old_cap", admission_records.CapUnavailable(())), ("missing_cap", admission_records.CapUnavailable((missing,)))):
            readings[name] = replace(base, explanation=replace(base.explanation, room=replace(room, cap=cap)))
        missing_room = replace(room, cap=admission_records.CapUnavailable((missing,)),
            runs=(replace(room.runs[0], memory=missing),))
        readings["missing_use"] = replace(base, explanation=replace(base.explanation, room=missing_room))
        pages: dict[str, object] = {}
        for name, reading in readings.items():
            with mock.patch.object(admission_client, "view", return_value=reading):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        for name, units in (("queued_none", ()), ("queued_one", ("research",)),
                            ("queued_two", ("research", "cables"))):
            with mock.patch.object(admission_client, "view", return_value=queued_reading(base, units)):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        readings["priority"] = with_queue_priority(base)
        with mock.patch.object(admission_client, "view", return_value=readings["priority"]):
            pages["priority_unmatched"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        labels = {base.view.running[0].session_id: unit_labels.UnitLabels("fixture", "fixture-production", "research")}
        with (mock.patch.object(admission_client, "view", return_value=readings["priority"]),
              mock.patch.object(unit_labels, "unit_labels_for", return_value=labels)):
            pages["priority"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        _ = write_idle_config(fixture.root)
        _ = write_idle_alert(fixture.root / "mac-idle", base.explanation.now)
        with mock.patch.object(admission_client, "view", return_value=base), \
             mock.patch.object(time, "time", return_value=base.explanation.now):
            pages["mac_idle"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        (fixture.root / "mac-idle" / "alert.json").unlink()
        for name, report in (("empty_report", report_fixture(empty=True)),
                             ("missing_report", charts.IndexUnavailable("private index is missing"))):
            with (mock.patch.object(charts, "page_data", return_value=report),
                  mock.patch.object(admission_client, "view", return_value=base)):
                pages[name] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        mac_builds = tuple(queue_view.MacBuild(pid, path, base.explanation.now - 120 + index,
            command, repository, queue_view.MacBuilding(), int(used * admission_records.GIB),
            queue_view.mac_command_jobs(command), progress_profile.ProgressCounter("build:1", done, 1000), unit)
            for index, (pid, path, command, repository, used, done, unit) in enumerate((
                (30081, "/private/tmp/trunk-p172-assembly-rows", "cargo build --release -p hana", "hana", 0.6, 604, "trunk"),
                (34606, "/Users/nate/.local/state/mac-test/mirror/hana", "cargo nextest run --test-threads 12", "hana", 0.3, 647, "verify"),
                (42846, "/Users/nate/.local/state/mac-test/mirror/cargo-liner", "cargo nextest run --jobs=6", "cargo-liner", 0.3, 983, "handler"))))
        mac_reading = queue_view.MacBuildReading(base.explanation.now - 5, mac_builds)
        from scripts.mac_test import claim_state
        from scripts.copies import copies
        mac_claim: claim_state.RemoteJobClaim = {"claim_id": "fixture", "episode": "fixture", "attempt": "fixture",
            "submitting_session": "claim-owner", "pid": 900, "proc_start": "start", "what": "nextest hana",
            "worktree": mac_builds[1].folder, "since": "2026-11-12T18:00:00+00:00", "remote_host": "mac", "repo_name": "hana"}
        claim = claim_state.HeldClaim(claim_state.RepositoryScope("hana"), fixture.root / "claim.json", mac_claim)
        with (mock.patch.object(admission_client, "view", return_value=base),
              mock.patch.object(queue_view, "read_mac_builds", return_value=mac_reading),
              mock.patch.object(claim_state, "read_claims", return_value=(claim,)),
              mock.patch.object(copies, "held_remote_mirrors", return_value=()),
              mock.patch.object(unit_labels, "unit_labels_for", return_value={"claim-owner": unit_labels.UnitLabels("worker", "hana", "trunk")})):
            pages["mac_three"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        with (mock.patch.object(admission_client, "view", return_value=base),
              mock.patch.object(queue_view, "read_mac_builds", return_value=queue_view.MacBuildReading(
                  base.explanation.now - 540, mac_builds, "awk: private raw error"))):
            pages["mac_failed"] = json.loads(memory_feed.page_json(memory_feed.reading(configuration)))
        _ = (folder / "fixtures.json").write_text(json.dumps(pages))
    finally:
        _ = fixture.doCleanups()
