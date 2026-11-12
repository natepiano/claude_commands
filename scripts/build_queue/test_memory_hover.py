"""Check the memory page hover with private captured readings."""
from __future__ import annotations
from scripts.build_queue import admission_records

import json
from pathlib import Path
import shutil
import subprocess
import unittest
from xml.etree import ElementTree
from typing import cast
from scripts.lint import memory_admit as gate
from scripts.build_queue import memory_feed, progress_profile as progress
from scripts.build_queue import test_memory_feed as feed_tests
from scripts.build_queue.admission_records import reason_phrase
from scripts.lint.memory_test_support import admission_policy_fixture
from scripts.lint import test_memory_admit as memory_tests
from scripts.lib import test_hang_guard
GIB = admission_records.GIB


class MemoryHoverTests(unittest.TestCase):
    def test_the_memory_page_hover_shows_the_captured_phase_percent_hold_and_reason(self) -> None:
        row = memory_tests.ProgressKeepAsideTests.reservation("merge-gate", need=4 * GIB)
        memory = admission_records.MeasuredMemory(9 * GIB, anonymous=admission_records.AnonymousRunUse(GIB))
        adjusted = gate.progress_memory(row, memory, gate.AttributedStepUse(memory.anonymous, True),
            progress.ProgressCounter("suites:1", 8, 10), memory_tests.ProgressKeepAsideTests.profile("suites:1", 3 * GIB // 2),
            admission_policy_fixture())
        inputs = memory_tests.ProgressKeepAsideTests.inputs((row,), (adjusted,))
        room = gate.inputs_room(inputs, admission_policy_fixture())
        assert isinstance(room, admission_records.MemoryRoom)
        text = memory_feed.run_keep_text(room.runs[0], inputs.running)
        self.assertIn("suites:1, 80.0% done", text)
        self.assertIn("0.5 GiB", text)
        self.assertIn("highest anonymous use in the next 20.0%", text)
        module_counter = memory_feed.row_progress(inputs.running)
        assert module_counter["kind"] == "RowProgress"
        test_counter: memory_feed.RowProgress = {**module_counter, "unit": "tests"}
        for name, counter, ordering_note in (
            ("merge-gate", module_counter, True),
            ("merge gate", module_counter, True),
            ("fixture-project merge gate", module_counter, True),
            ("merge-gate", test_counter, False),
        ):
            with self.subTest(name=name, unit=counter["unit"]):
                hover = cast(list[str], self.page_expression("tipText('row:0', data)", {"walk": [{
                    "name": name, "unit": "fixture", "showrunner": "fixture-production",
                    "used": 1, "reserved": 0.5, "counted": 1.5, "queued": True, "keep_text": text,
                    "progress": counter}]}))
                self.assertEqual(hover[0], "Running step")
                for expected in ("suites:1, 80.0% done", "0.5 GiB", "highest anonymous use in the next 20.0%",
                                 "The queue holds this memory for the run.", "Unit: fixture",
                                 f"testing · 8 of 10 {counter['unit']} · 80.0% done"):
                    with self.subTest(expected=expected):
                        self.assertIn(expected, hover[1])
                self.assertEqual("Modules start slowest first, so this reads low early." in hover[1], ordering_note)


    def test_cloud_hover_shows_the_progress_and_reservation(self) -> None:
        for status, counter, expected in (
            ("7 of 12 modules", progress.ProgressCounter("modules", 7, 12), "58.3% done"),
            ("setup", progress.NoCounter("no cloud counter"), "No local memory reservation."),
            ("", progress.NoCounter("no cloud counter"), "No local memory reservation."),
        ):
            with self.subTest(status=status):
                hover = cast(list[str], self.page_expression("tipText('row:0', data)", {"walk": [{
                    "kind": "cloud", "machine": "aws", "name": "merge-gate", "unit": "cloud-unit", "showrunner": "fixture",
                    "status": status, "progress": memory_feed.cloud_row_progress(counter)}]}))
                self.assertIn(expected, hover[1])
                self.assertIn("No local memory reservation.", hover[1])
                self.assertIn("Machine: aws", hover[1])
                if status:
                    self.assertNotIn(status, hover[1])
                self.assertFalse(hover[1].startswith("."), hover[1])

    def test_progress_percent_rounds_down_until_the_counter_is_complete(self) -> None:
        for done, total, percent, expected in (
            (835, 836, 100 * 835 / 836, "99.8%"),
            (836, 836, 100, "100.0%"),
            (999, 1000, 100, "99.9%"),
            (0, 836, 0, "0.0%"),
        ):
            with self.subTest(done=done, total=total, percent=percent):
                text = self.page_expression("progressTip(data)", {"progress": {
                    "kind": "RowProgress", "phase": "building", "unit": "units",
                    "done": done, "total": total, "percent": percent}})
                self.assertIn(f"building · {done} of {total} units · {expected} done", str(text))

    def test_pressure_percent_uses_one_decimal_and_rounds_down(self) -> None:
        hover = self.page_expression("[tipText('g-some', data), tipText('g-full', data), tipText('g-swap', data)]", {
            "psi": {"some": 1.29, "full": 0.09}, "limits": {"some": 25, "full": 5, "swap": 85},
            "swap_used": 1, "swap_total": 3})
        for expected in ("1.2%", "0.0%", "33.3%", "25.0%", "5.0%", "85.0%"):
            self.assertIn(expected, str(hover))

    def test_chart_rows_show_time_without_a_step_table(self) -> None:
        for waiting in (False, True):
            with self.subTest(waiting=waiting):
                running: list[dict[str, object]] = [{"name": phase, "queued": True, "unit": "fixture", "pid": 10 + index,
                            "machine": "fixture", "vcpus": 4, "started_at": 80,
                            "progress": {"kind": "RowProgress", "phase": phase}}
                           for index, phase in enumerate(("building", "testing"))]
                running.append({"name": "no counter", "queued": True, "started_at": 80,
                                "progress": {"kind": "NoProgress"}})
                waiter = {"name": "waiting", "since": 200 - 2 * 86400, "detail": "fixture", "pid": 20,
                          "progress": {"kind": "RowProgress", "phase": "building"}}
                data = {"total": 64, "cap": 48, "taken": 4, "read_at": 200,
                        "walk": running, "waiters": [waiter] if waiting else []}
                rendered = self.page_expression("(renderWalk(data), $('walk').innerHTML)", data, walk=True)
                root = ElementTree.fromstring(f"<root>{rendered}</root>")
                self.assertIsNone(root.find(".//table"))
                for index in range(3):
                    row = root.find(f".//div[@data-run='{index}']")
                    assert row is not None
                    duration = row.find(".//span[@class='row-time']")
                    assert duration is not None
                    self.assertEqual(duration.text, "2 min")
                spacers = root.findall("./div[@class='walk-chart']/div[@class='walk-chart-head queue-head']")
                self.assertEqual(spacers, [])
                if waiting:
                    row = root.find(".//div[@data-tip='wait:0']")
                    assert row is not None
                    duration = row.find(".//span[@class='row-time']")
                    assert duration is not None
                    self.assertEqual(duration.text, "waits 2 days")

    def page_expression(self, expression: str, data: object, *, walk: bool = False) -> object:
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        functions = html[html.index("const TIPS ="):html.index("const tip =")]
        constants = html[html.index("const f1 ="):html.index("function setSeg(")]
        if walk:
            functions += html[html.index("function renderWalk(d)"):html.index("function fitAxisLabels()")]
            functions += html[html.index("function esc(s)"):html.index("let lastRead =")]
            constants += """
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {style: {}, classList: {toggle() {}}});
  return elements.get(id);
};
function fitLabels() {}
"""
        javascript = functions + constants + "\nconst data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" + (
            "process.stdout.write(JSON.stringify(" + expression + "));\n")
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        result = subprocess.run([executable, "-e", javascript], input=json.dumps(data),
            text=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return cast(object, json.loads(result.stdout))

    def test_each_wait_state_has_a_page_hover_reason(self) -> None:
        for reason, expected in (
            (admission_records.PressureUnavailable("cannot read /private/proc/pressure/memory"), "/private/proc/pressure/memory"),
            (admission_records.StartSpacingWait(110), "start spacing"),
            (admission_records.BehindHead("first", 1, "head-session"), "behind"),
        ):
            with self.subTest(reason=reason):
                reading = feed_tests.with_wait_reason(feed_tests.captured_reading(), reason)
                waiter = reading.view.waiting[0]
                hover = self.page_expression("tipText('wait:0', data)", {"read_at":200_000, "waiters":[{
                    "name":waiter.step, "unit":"", "showrunner":"", "need":1, "since":waiter.since,
                    "reason":reason_phrase(reason, reading.view.waiting)}]})
                self.assertIn(expected, str(hover))

    def test_missing_run_use_has_a_page_hover_reason(self) -> None:
        hover = self.page_expression("tipText('row:0', data)", {"read_at":200_000, "walk":[{
            "name":"fixture", "unit":"", "showrunner":"", "used":{
                "kind":"RunUseUnavailable", "reason":"private memory.stat is missing"},
            "reserved":4, "counted":4, "queued":True, "keep_text":"full reservation", "progress":{
                "kind":"NoProgress", "reason":"no counter"}}]})
        self.assertIn("private memory.stat is missing", str(hover))
        self.assertNotIn("NaN", str(hover))

    def test_missing_pressure_keeps_the_other_limit_readings(self) -> None:
        result = self.page_expression("[level(data), tipText('g-full', data), tipText('cap', data)]", {
            "psi":{"some":{"kind":"PressureNotRecorded", "reason":"private PSI is missing"}, "full":1},
            "limits":{"some":25,"full":5,"swap":85}, "pressure":"none", "cap":20,
            "swap_used":0,"swap_total":8,"cap_text":"fixture room"})
        self.assertIn("neutral", str(result))
        self.assertIn("1.0", str(result))
        self.assertIn("20.0 GiB", str(result))
