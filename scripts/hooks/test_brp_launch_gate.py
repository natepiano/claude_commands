"""BRP launch admission uses the shared memory gate and a temporary hold cycle."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, override


HOOK = Path(__file__).with_name("pre-tool-use-brp-launch-gate.sh")
SETTINGS = HOOK.parent.parent.parent / "settings.json"
CYCLE_ID = "a" * 32


class BrpLaunchGateTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}
    cycle_file: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holders = self.root / "holders"
        release = self.root / "release"
        holders.mkdir()
        (release / CYCLE_ID).mkdir(parents=True)
        _ = (holders / "holder").write_text(json.dumps({
            "holder": "holder", "since": datetime.now().astimezone().isoformat(),
            "for": "the test", "release_eta": "unknown",
        }) + "\n")
        _ = (release / "current").write_text(CYCLE_ID + "\n")
        self.cycle_file = release / CYCLE_ID / "cycle.json"
        self.write_cycle()
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "BUILD_HOLD_DIR": str(holders),
            "BUILD_HOLD_RELEASE_DIR": str(release),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_MEM_POLL_S": "1",
        }

    def write_cycle(self) -> None:
        now = datetime.now().astimezone().isoformat()
        cycle: dict[str, object] = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": now,
            "holders": {"holder": {"since": now, "released_at": ""}},
            "recipients": {"unit-seat": "unit"},
            "entries": [{
                "session_id": "unit-seat", "name": "unit", "state": "ReleasedAwaitingAdmission",
                "released_at": now,
            }],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle) + "\n")

    def entry(self) -> dict[str, object]:
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        return entries[0]

    def run_hook(self, session_id: str | None = "unit-seat") -> subprocess.CompletedProcess[str]:
        hook_input = {"tool_name": "mcp__brp__brp_launch"}
        if session_id is not None:
            hook_input["session_id"] = session_id
        return subprocess.run(
            ["bash", str(HOOK)], input=json.dumps(hook_input),
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )

    def test_low_memory_marks_wait_then_return_before_launch(self) -> None:
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
        stubs = self.root / "stubs"
        stubs.mkdir()
        sleeper = stubs / "sleep"
        _ = sleeper.write_text(r'''#!/bin/sh
python3 - "$CYCLE_FILE" "$WAIT_STATE_LOG" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["entries"][0]
with open(sys.argv[2], "a") as output:
    output.write(entry["state"] + "\n")
PY
printf "MemAvailable: 67108864 kB\n" > "$BUILDLOG_MEMINFO"
''')
        sleeper.chmod(0o755)
        self.environment["PATH"] = f"{stubs}:{os.environ['PATH']}"
        self.environment["CYCLE_FILE"] = str(self.cycle_file)
        self.environment["WAIT_STATE_LOG"] = str(self.root / "wait-states")

        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("waiting for memory", result.stderr)
        self.assertEqual((self.root / "wait-states").read_text().splitlines(), ["WaitingForMemory"])
        entry = self.entry()
        self.assertEqual(entry["state"], "MemoryGateReturned")
        self.assertEqual(entry["outcome"], "Granted")
        ended = datetime.fromisoformat(cast(str, entry["wait_ended_at"]))
        self.assertIsNotNone(ended.tzinfo)
        self.assertNotIn("wait_started_at", entry)
        self.assertEqual(self.entry()["state"], "MemoryGateReturned")

    def test_timeout_and_unavailable_memory_have_distinct_outcomes(self) -> None:
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
        self.environment["BUILDLOG_MEM_WAIT_LIMIT_S"] = "0"
        timed_out = self.run_hook()
        self.assertEqual(timed_out.returncode, 0, timed_out.stderr)
        self.assertEqual(self.entry()["outcome"], "TimedOut")

        self.write_cycle()
        _ = meminfo.write_text("MemFree: 8388608 kB\n")
        unavailable = self.run_hook()
        self.assertEqual(unavailable.returncode, 0, unavailable.stderr)
        self.assertEqual(self.entry()["outcome"], "MeminfoUnavailable")
        self.assertNotIn("wait_started_at", self.entry())

    def test_no_session_id_waits_for_memory_without_marking(self) -> None:
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
        self.environment["BUILDLOG_MEM_WAIT_LIMIT_S"] = "1"
        self.environment["CLAUDE_CODE_SESSION_ID"] = "unit-seat"
        before = self.cycle_file.read_text()
        result = self.run_hook(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("waiting for memory", result.stderr)
        self.assertIn("memory wait limit reached", result.stderr)
        self.assertEqual(self.cycle_file.read_text(), before)

    def test_settings_registers_hook_with_long_enough_timeout(self) -> None:
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        registrations = [item for item in hooks["PreToolUse"] if item.get("matcher") == "mcp__brp__brp_launch"]
        self.assertEqual(len(registrations), 1)
        commands = cast(list[dict[str, object]], registrations[0]["hooks"])
        self.assertEqual(len(commands), 1)
        self.assertIn("pre-tool-use-brp-launch-gate.sh", cast(str, commands[0]["command"]))
        self.assertGreater(cast(int, commands[0]["timeout"]), 900)


if __name__ == "__main__":
    _ = unittest.main()
