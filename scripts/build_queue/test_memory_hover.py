"""Check the memory page hover with private captured readings."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from typing import cast
from scripts.lint import memory_admit as gate
from scripts.build_queue import memory_feed, progress_profile as progress
from scripts.lint.memory_test_support import admission_policy_fixture
from scripts.lint import test_memory_admit as memory_tests
from scripts.lib import test_hang_guard
GIB = gate.GIB


class MemoryHoverTests(unittest.TestCase):
    def test_the_memory_page_hover_shows_the_captured_phase_percent_hold_and_reason(self) -> None:
        row = memory_tests.ProgressKeepAsideTests.reservation("merge-gate", need=4 * GIB)
        memory = gate.MeasuredMemory(9 * GIB, anonymous=gate.AnonymousRunUse(GIB))
        adjusted = gate.progress_memory(row, memory, gate.AttributedStepUse(memory.anonymous, True),
            progress.ProgressCounter("suites:1", 8, 10), memory_tests.ProgressKeepAsideTests.profile("suites:1", 3 * GIB // 2),
            admission_policy_fixture())
        inputs = memory_tests.ProgressKeepAsideTests.inputs((row,), (adjusted,))
        room = gate.inputs_room(inputs, admission_policy_fixture())
        assert isinstance(room, gate.MemoryRoom)
        text = memory_feed.run_keep_text(room.runs[0], inputs.running)
        self.assertIn("suites:1, 80% done", text)
        self.assertIn("0.5 GiB", text)
        self.assertIn("highest anonymous use in the next 20%", text)
        html = Path(memory_feed.__file__).with_name("memory_live.html").read_text()
        functions = html[html.index("function visibleDetail("):html.index("const tip =")]
        javascript = functions + "\nconst f1 = (n) => n.toFixed(1);\n" + (
            "const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" +
            "process.stdout.write(JSON.stringify(tipText('row:0', data)));\n")
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        result = subprocess.run([executable, "-e", javascript], input=json.dumps({"walk": [{
            "name": "merge-gate", "unit": "fixture", "showrunner": "fixture-production",
            "used": 1, "reserved": 0.5, "counted": 1.5, "queued": True, "keep_text": text,
            "progress": memory_feed.row_progress(inputs.running)}]}),
            text=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hover = cast(list[str], json.loads(result.stdout))
        self.assertEqual(hover[0], "merge-gate · fixture-production")
        for expected in ("suites:1, 80% done", "0.5 GiB", "highest anonymous use in the next 20%",
                         "The queue holds this memory for the run.", "\nfixture",
                         "testing · 8 of 10 modules · 80% done"):
            with self.subTest(expected=expected):
                self.assertIn(expected, hover[1])

