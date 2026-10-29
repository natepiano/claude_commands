"""Released build steps use memory admission without changing hold records."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.build_hold import build_hold
from scripts.direct import test_verify_untested_examples
from scripts.direct.test_verify_hold_wait import read_cycle_file, release_registered_call
from scripts.lint.memory_test_support import fake_memory_readings


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

INVOKE = Path(__file__).resolve().parent.parent / "lint" / "invoke.sh"
CYCLE_ID = "b" * 32


class VerifyReleaseAckTests(unittest.TestCase):
    root: Path = Path()
    cycle_file: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holders = self.root / "holders"
        release = self.root / "release"
        (release / CYCLE_ID).mkdir(parents=True)
        holders.mkdir()
        now = datetime.now().astimezone().isoformat()
        _ = (holders / "holder").write_text(json.dumps({
            "holder": "holder", "since": now, "for": "the test", "release_eta": "unknown",
        }) + "\n")
        _ = (release / "current").write_text(CYCLE_ID + "\n")
        self.cycle_file = release / CYCLE_ID / "cycle.json"
        self.reset_cycle()
        memory_environment = fake_memory_readings(self.root)
        lint_reader = self.root / "lint-config.sh"
        _ = lint_reader.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
        stubs = self.root / "stubs"
        tool_path = test_verify_untested_examples.rust_tool_path(stubs)
        recorder = stubs / "compile"
        _ = recorder.write_text(r'''#!/bin/sh
python3 - "$CYCLE_FILE" "$STEP_STATE_LOG" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["entries"][0]
with open(sys.argv[2], "a") as output:
    output.write(entry["delivery"] + "\n")
PY
''')
        recorder.chmod(0o755)
        sweep = stubs / "sweep.py"
        _ = sweep.write_text('#!/bin/sh\nexit 0\n')
        sweep.chmod(0o755)
        (holders / "holder").unlink()
        self.environment = {
            **{name: value for name, value in os.environ.items()
               if name not in {"CODEX_THREAD_ID", "CODEX_SESSION_ID", "UNIT_DETACHED_RUN"}},
            "HOME": str(self.root),
            "PATH": tool_path,
            "TMPDIR": str(self.root),
            "XDG_RUNTIME_DIR": str(self.root),
            "BUILD_HOLD_DIR": str(holders),
            "BUILD_HOLD_RELEASE_DIR": str(release),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILD_TARGETS_SCRIPT": str(INVOKE.parent.parent / "build_targets" / "build_targets.py"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "",
            "UNIT_SESSION_DIR": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            **memory_environment,
            "BUILDLOG_MEM_POLL_S": "1",
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "CLAUDE_CODE_SESSION_ID": "unit-director",
            "LINT_CONFIG_READER": str(lint_reader),
            "CYCLE_FILE": str(self.cycle_file),
            "STEP_STATE_LOG": str(self.root / "step-states"),
            "INVOKE_SCRIPT": str(INVOKE),
            "SWEEP_SCRIPT": str(sweep),
        }

    def reset_cycle(self) -> None:
        now = datetime.now().astimezone().isoformat()
        cycle: dict[str, object] = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": now,
            "holders": {"holder": {"since": now, "released_at": ""}},
            "recipients": {"unit-director": "unit"},
            "entries": [{
                "session_id": "unit-director", "name": "unit", "delivery": "Delivered",
            }],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle) + "\n")

    def entry(self) -> dict[str, object]:
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        return entries[0]

    def invoke(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", 'set -euo pipefail; source "$INVOKE_SCRIPT"; sweep_after_step() { :; }; run_once "$@"', "bash", *command],
            cwd=self.root,
            env=self.environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def set_owner_marker(self) -> None:
        run = self.root / "unit-run"
        active = Path(self.environment["UNIT_ACTIVE_DIR"])
        active.mkdir()
        _ = (active / "unit-director").write_text(str(run) + "\n")
        self.environment["UNIT_RUN_DIR"] = str(run)
        self.environment["CLAUDE_CODE_SESSION_ID"] = "stale-session"

    def release_memory_after_poll(self) -> None:
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 8388608 kB\n")
        sleeper = self.root / "stubs" / "sleep"
        sleeper.unlink()
        _ = sleeper.write_text(r'''#!/bin/sh
python3 - "$CYCLE_FILE" "$WAIT_STATE_LOG" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["entries"][0]
with open(sys.argv[2], "a") as output:
    output.write(json.dumps(entry) + "\n")
PY
printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"
''')
        sleeper.chmod(0o755)
        self.environment["WAIT_STATE_LOG"] = str(self.root / "wait-states")










    def test_memory_wait_and_compile_leave_release_record_unchanged(self) -> None:
        self.release_memory_after_poll()
        before = self.cycle_file.read_bytes()
        result = self.invoke(str(self.root / "stubs" / "compile"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("waiting for memory", result.stderr)
        self.assertEqual(self.cycle_file.read_bytes(), before)
        self.assertEqual((self.root / "step-states").read_text().splitlines(), ["Delivered"])
        self.assertEqual(json.loads((self.root / "wait-states").read_text()), self.entry())

    def test_immediate_memory_grant_leaves_release_record_unchanged(self) -> None:
        self.set_owner_marker()
        before = self.cycle_file.read_bytes()
        result = self.invoke(str(self.root / "stubs" / "compile"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cycle_file.read_bytes(), before)
        self.assertEqual((self.root / "step-states").read_text().splitlines(), ["Delivered"])


class VerifyRoutingTests(unittest.TestCase):
    fixture: test_verify_untested_examples.UntestedExampleTests
    cycle_file: Path = Path()

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = test_verify_untested_examples.UntestedExampleTests("test_clean_package_passes_gate")

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        root = self.fixture.root
        self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(root / "buildlog")}))
        holders = root / "build-hold"
        release = root / "release"
        (release / CYCLE_ID).mkdir(parents=True)
        holders.mkdir()
        now = datetime.now().astimezone().isoformat()
        _ = (holders / "holder").write_text(json.dumps({
            "holder": "holder", "since": now, "for": "the test", "release_eta": "unknown",
        }) + "\n")
        _ = (release / "current").write_text(CYCLE_ID + "\n")
        self.cycle_file = release / CYCLE_ID / "cycle.json"
        self.reset_cycle()
        self.fixture.environment.update({
            "HOME": str(root),
            "CLAUDE_PYTHON": sys.executable,
            "BUILDLOG_DIR": str(root / "buildlog"),
            "BUILD_HOLD_DIR": str(holders),
            "BUILD_HOLD_RELEASE_DIR": str(release),
            "UNIT_ACTIVE_DIR": str(root / "active"),
            "UNIT_RUN_DIR": "",
            "UNIT_TEAM_ROLE": "",
            "NOTIFIER_SESSIONS_DIR": str(root / "sessions"),
            "MAC_TEST_STATE_DIR": str(root / "mac-state"),
            "VERIFY_MAC_RUNNER": str(root / "no-mac-runner"),
            "CLAUDE_CODE_SESSION_ID": "unit-director",
        })
        self.fixture.environment["BUILD_TARGETS_POLL_S"] = "0.01"
        self.fixture.environment["BUILD_TARGETS_NOW"] = ""
        _ = self.enterContext(mock.patch.object(build_hold, "start_recorder", side_effect=self.record_hold_event))

    def record_hold_event(self, command: list[str]) -> None:
        _ = subprocess.run(command, env=self.fixture.environment, capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)

    def set_owner_marker(self) -> None:
        run = self.fixture.root / "unit-run"
        run.mkdir()
        active = Path(self.fixture.environment["UNIT_ACTIVE_DIR"])
        active.mkdir()
        _ = (active / "unit-director").write_text(str(run) + "\n")
        self.fixture.environment["UNIT_RUN_DIR"] = str(run)
        self.fixture.environment["CLAUDE_CODE_SESSION_ID"] = "stale-session"

    def reset_cycle(self) -> None:
        now = datetime.now().astimezone().isoformat()
        cycle: dict[str, object] = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": "",
            "holders": {"holder": {"since": now, "released_at": ""}},
            "recipients": {"unit-director": "unit"},
            "entries": [{
                "session_id": "unit-director", "name": "unit", "delivery": "NotAttempted",
            }],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle) + "\n")

    def state(self) -> str:
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        return cast(str, entries[0]["delivery"])

    def test_example_gate_refusal_writes_no_admission_mark(self) -> None:
        _ = self.fixture.package()
        _ = self.fixture.example(source="#[test]\nfn check() {}\nfn main() {}\n")

        result = self.fixture.verify("test", "sample")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("examples carry no tests", result.stderr)
        self.assertEqual(self.state(), "NotAttempted")
        self.assertEqual(self.fixture.calls_made(), [])

    def test_recorded_pass_writes_no_second_admission_mark(self) -> None:
        _ = self.fixture.package()
        _ = self.fixture.example(source="fn main() {}\n")
        with release_registered_call(self.cycle_file):
            first = self.fixture.verify("test", "sample")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.state(), "Delivered")
        rows = [cast(dict[str, object], json.loads(line)) for path in (self.fixture.root / "buildlog").rglob("*.jsonl")
                for line in path.read_text().splitlines()]
        hold_rows = [row for row in rows if row.get("kind") == "hold_event"]
        self.assertTrue(hold_rows, "The release must write a hold row to the private build log")
        self.assertTrue(all(row["holder"] == "holder" and row["reason"] == "the test" for row in hold_rows))
        self.assertEqual({row["event"] for row in hold_rows}, {"ended"})

        self.reset_cycle()
        second = self.fixture.verify("test", "sample")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("PASS (recorded)", second.stdout)
        self.assertEqual(self.state(), "NotAttempted")

    def test_worker_with_stale_shell_id_builds_after_release(self) -> None:
        _ = self.fixture.package()
        _ = self.fixture.example(source="fn main() {}\n")
        self.set_owner_marker()
        with release_registered_call(self.cycle_file):
            result = self.fixture.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.fixture.calls_made()))
        self.assertEqual(self.state(), "Delivered")
        entries = read_cycle_file(self.cycle_file)["entries"]
        self.assertEqual(entries[0]["session_id"], "unit-director")
        self.assertNotIn("outcome", entries[0])

    def test_ownerless_call_runs_after_release_without_a_call_record(self) -> None:
        _ = self.fixture.package()
        _ = self.fixture.example(source="fn main() {}\n")
        self.fixture.environment.update({
            "CLAUDE_CODE_SESSION_ID": "", "UNIT_RUN_DIR": "",
            "UNIT_SESSION_DIR": "",
        })
        cycle = read_cycle_file(self.cycle_file)
        cycle["entries"] = []
        cycle["recipients"] = {}
        _ = self.cycle_file.write_text(json.dumps(cycle))
        with release_registered_call(self.cycle_file):
            result = self.fixture.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(any(call.startswith("nextest run ") for call in self.fixture.calls_made()))
        entries = read_cycle_file(self.cycle_file)["entries"]
        self.assertEqual(entries, [])

    def test_ownerless_example_refusal_performs_no_admission(self) -> None:
        _ = self.fixture.package()
        _ = self.fixture.example(source="#[test]\nfn check() {}\nfn main() {}\n")
        self.fixture.environment.update({
            "CLAUDE_CODE_SESSION_ID": "", "UNIT_RUN_DIR": "",
            "UNIT_SESSION_DIR": "",
        })
        before = self.cycle_file.read_bytes()
        result = self.fixture.verify("test", "sample")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.cycle_file.read_bytes(), before)
        self.assertEqual(self.fixture.calls_made(), [])


if __name__ == "__main__":
    _ = unittest.main()
