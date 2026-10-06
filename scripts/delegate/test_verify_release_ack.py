"""Compiling steps acknowledge admission after the shared memory gate returns."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, override

from scripts.delegate import test_verify_untested_examples


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
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        lint_reader = self.root / "lint-config.sh"
        _ = lint_reader.write_text('lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n')
        stubs = self.root / "stubs"
        stubs.mkdir()
        recorder = stubs / "compile"
        _ = recorder.write_text(r'''#!/bin/sh
python3 - "$CYCLE_FILE" "$STEP_STATE_LOG" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["entries"][0]
with open(sys.argv[2], "a") as output:
    output.write(entry["state"] + "\n")
PY
''')
        recorder.chmod(0o755)
        cargo = stubs / "cargo"
        _ = cargo.write_text(recorder.read_text())
        cargo.chmod(0o755)
        sweep = stubs / "sweep.py"
        _ = sweep.write_text('#!/bin/sh\nexit 0\n')
        sweep.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "TMPDIR": str(self.root),
            "XDG_RUNTIME_DIR": str(self.root),
            "BUILD_HOLD_DIR": str(holders),
            "BUILD_HOLD_RELEASE_DIR": str(release),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_MEM_POLL_S": "1",
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
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
                "session_id": "unit-director", "name": "unit", "state": "ReleasedAwaitingAdmission",
                "released_at": now,
            }],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle) + "\n")

    def entry(self) -> dict[str, object]:
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        return entries[0]

    def invoke(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", 'source "$INVOKE_SCRIPT"; sweep_after_step() { :; }; run_once "$@"', "bash", *command],
            env=self.environment, capture_output=True, text=True, check=False, timeout=10,
        )

    def test_wait_start_and_return_precede_compiling_command(self) -> None:
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 8388608 kB\n")
        sleeper = self.root / "stubs" / "sleep"
        _ = sleeper.write_text(r'''#!/bin/sh
python3 - "$CYCLE_FILE" "$WAIT_STATE_LOG" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["entries"][0]
with open(sys.argv[2], "a") as output:
    output.write(json.dumps({"state": entry["state"], "wait_started_at": entry.get("wait_started_at")}) + "\n")
PY
printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"
''')
        sleeper.chmod(0o755)
        self.environment["WAIT_STATE_LOG"] = str(self.root / "wait-states")

        result = self.invoke(str(self.root / "stubs" / "compile"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        waiting = cast(dict[str, object], json.loads((self.root / "wait-states").read_text()))
        self.assertEqual(waiting["state"], "WaitingForMemory")
        self.assertIsNotNone(datetime.fromisoformat(cast(str, waiting["wait_started_at"])).tzinfo)
        self.assertEqual((self.root / "step-states").read_text().splitlines(), ["MemoryGateReturned"])
        self.assertEqual(self.entry()["outcome"], "Granted")
        self.assertTrue(self.entry()["wait_ended_at"])
        self.assertNotIn("wait_started_at", self.entry())
        self.assertNotIn("released_at", self.entry())

    def test_immediate_grant_marks_only_return(self) -> None:
        result = self.invoke(str(self.root / "stubs" / "compile"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.entry()["state"], "MemoryGateReturned")
        self.assertEqual(self.entry()["outcome"], "Granted")
        self.assertNotIn("wait_started_at", self.entry())
        self.assertNotIn("released_at", self.entry())

    def test_sweep_and_cargo_fmt_write_no_mark(self) -> None:
        for command in ((self.environment["SWEEP_SCRIPT"],), ("cargo", "+nightly", "fmt")):
            with self.subTest(command=command):
                self.reset_cycle()
                result = self.invoke(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.entry()["state"], "ReleasedAwaitingAdmission")

    def test_seat_uses_director_session_id_and_missing_id_writes_nothing(self) -> None:
        result = self.invoke(str(self.root / "stubs" / "compile"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.entry()["session_id"], "unit-director")
        self.assertEqual(self.entry()["state"], "MemoryGateReturned")

        self.reset_cycle()
        self.environment["CLAUDE_CODE_SESSION_ID"] = ""
        no_id = self.invoke(str(self.root / "stubs" / "compile"))
        self.assertEqual(no_id.returncode, 0, no_id.stdout + no_id.stderr)
        self.assertEqual(self.entry()["state"], "ReleasedAwaitingAdmission")


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
        scripts = Path(__file__).resolve().parent.parent
        claude = root / ".claude"
        claude.mkdir()
        (claude / "scripts").symlink_to(scripts, target_is_directory=True)
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
            "REAL_CARGO": subprocess.check_output(["rustup", "which", "cargo"], text=True).strip(),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
            "BUILD_HOLD_DIR": str(holders),
            "BUILD_HOLD_RELEASE_DIR": str(release),
            "CLAUDE_CODE_SESSION_ID": "unit-director",
        })

    def reset_cycle(self) -> None:
        now = datetime.now().astimezone().isoformat()
        cycle: dict[str, object] = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": now,
            "holders": {"holder": {"since": now, "released_at": ""}},
            "recipients": {"unit-director": "unit"},
            "entries": [{
                "session_id": "unit-director", "name": "unit", "state": "ReleasedAwaitingAdmission",
                "released_at": now,
            }],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle) + "\n")

    def state(self) -> str:
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        return cast(str, entries[0]["state"])

    def test_example_gate_refusal_writes_no_admission_mark(self) -> None:
        _ = self.fixture.package()
        _ = self.fixture.example(source="#[test]\nfn check() {}\nfn main() {}\n")

        result = self.fixture.verify("test", "sample")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("examples carry no tests", result.stderr)
        self.assertEqual(self.state(), "ReleasedAwaitingAdmission")
        self.assertEqual(self.fixture.calls_made(), [])

    def test_recorded_pass_writes_no_second_admission_mark(self) -> None:
        _ = self.fixture.package()
        _ = self.fixture.example(source="fn main() {}\n")
        first = self.fixture.verify("test", "sample")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.state(), "MemoryGateReturned")

        self.reset_cycle()
        second = self.fixture.verify("test", "sample")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("PASS (recorded)", second.stdout)
        self.assertEqual(self.state(), "ReleasedAwaitingAdmission")


if __name__ == "__main__":
    _ = unittest.main()
