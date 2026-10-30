"""Real verification hold waits: memory."""

from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.direct.verify_hold_wait_test_support import (
    StepObservation,
    VERIFY,
    VerifyHoldWaitFixture,
    WaitObservation,
    grant_call,
    read_cycle_file,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def test_holder_joining_during_memory_admission_delays_step_and_records_separate_waits(self) -> None:
        invoke = VERIFY.parent.parent / "lint" / "invoke.sh"
        memory_clock = self.fixture.root / "memory-clock"
        _ = memory_clock.write_text("100")
        compiled = self.fixture.root / "compiled"
        observation = self.fixture.root / "memory-step"
        waits = self.fixture.root / "memory-hold-seconds"
        environment = {
            **self.fixture.environment,
            "BUILDLOG_CALLER": "verify", "BUILDLOG_CALL_ID": "memory-call",
            "BUILDLOG_HOLD_WAIT_FILE": str(waits),
            "BUILDLOG_HOLD_WAITER_FILE": str(self.fixture.root / "memory-waiter"),
            "TEST_MEMORY_CLOCK": str(memory_clock), "TEST_COMPILED": str(compiled),
            "TEST_MEMORY_STEP": str(observation), "TEST_CYCLE_FILE": str(self.cycle_file),
        }
        script = '''set -euo pipefail
source "$1"
memory_calls=0
buildlog_begin() { BUILDLOG_START=100; BUILDLOG_PEAK=""; BUILDLOG_MEM_WAIT_S=0; }
buildlog_now() { BUILDLOG_NOW=$(cat "$TEST_MEMORY_CLOCK"); }
buildlog_wait_for_memory() {
    memory_calls=$((memory_calls + 1))
    if [[ "$memory_calls" == 1 ]]; then
        python3 - "$TEST_CYCLE_FILE" <<'PY_HOLD'
import json, os, sys
from datetime import datetime
from pathlib import Path
path = Path(sys.argv[1])
now = datetime.now().astimezone().isoformat()
holders = Path(os.environ["BUILD_HOLD_DIR"])
holders.mkdir()
(holders / "hana").write_text(json.dumps({"holder": "hana", "since": now, "for": "memory admission", "release_eta": "unknown"}))
path.parent.mkdir(parents=True)
(path.parent.parent / "current").write_text(path.parent.name)
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps({"id": path.parent.name, "opened_at": now, "release_started_at": "", "holders": {"hana": {"since": now, "released_at": ""}}, "recipients": {}, "entries": []}))
os.replace(temporary, path)
PY_HOLD
        BUILDLOG_MEM_WAIT_S=2
    else
        BUILDLOG_MEM_WAIT_S=3
    fi
    BUILDLOG_MEM_OUTCOME=Granted
    BUILDLOG_MEM_RESERVATION=""
}
buildlog_exec() { : > "$TEST_COMPILED"; }
buildlog_end() { printf '%s|%s|%s\\n' "$BUILDLOG_START" "$BUILDLOG_MEM_WAIT_S" "$memory_calls" > "$TEST_MEMORY_STEP"; }
sweep_after_step() { :; }
run_once cargo check
'''
        process = subprocess.Popen(
            ["bash", "-c", script, "bash", str(invoke)], cwd=self.fixture.root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        _ = self.wait_for_call(process)
        self.assertFalse(compiled.exists())
        _ = memory_clock.write_text("200")
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertTrue(compiled.exists())
        self.assertEqual(observation.read_text().strip(), "200|5|2")
        self.assertGreater(sum(float(line) for line in waits.read_text().splitlines()), 0)

    def test_memory_admission_hold_wait_is_recorded_by_verify_after_release(self) -> None:
        observation = self.observe_waits()
        observer = Path(self.fixture.environment["CLAUDE_PYTHON"])
        source = observer.read_text().replace(
            'os.execv(os.environ["TEST_REAL_PYTHON"],',
            '''if len(sys.argv) > 2 and sys.argv[1].endswith("/lint/memory_admit.py") and sys.argv[2] == "check":
    path = Path(os.environ["TEST_CYCLE_FILE"])
    if not path.exists():
        from datetime import datetime
        now = datetime.now().astimezone().isoformat()
        holders = Path(os.environ["BUILD_HOLD_DIR"])
        holders.mkdir()
        (holders / "hana").write_text(json.dumps({"holder": "hana", "since": now, "for": "memory admission", "release_eta": "unknown"}))
        path.parent.mkdir(parents=True)
        (path.parent.parent / "current").write_text(path.parent.name)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"id": path.parent.name, "opened_at": now, "release_started_at": "", "holders": {"hana": {"since": now, "released_at": ""}}, "recipients": {}, "entries": []}))
        os.replace(temporary, path)
os.execv(os.environ["TEST_REAL_PYTHON"],''',
        )
        _ = observer.write_text(source)
        self.fixture.environment.update({
            "TEST_CYCLE_FILE": str(self.cycle_file), "BUILDLOG_OFF": "0", "BUILDLOG_SYNC": "1",
        })
        process = self.start("test", "sample", "--local", "memory admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        release = datetime.fromisoformat(read_cycle_file(self.cycle_file)["release_started_at"]).timestamp()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        records = [cast(StepObservation, json.loads(line)) for line in (self.fixture.root / "step-observations").read_text().splitlines()]
        nextest = [record for record in records if record["arguments"][:2] == ["cargo", "nextest"]]
        self.assertEqual(len(nextest), 1)
        self.assertGreaterEqual(float(nextest[0]["start"]), release)
        self.assertEqual(float(nextest[0]["mem_wait"]), 0)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)


if __name__ == "__main__":
    _ = unittest.main()
