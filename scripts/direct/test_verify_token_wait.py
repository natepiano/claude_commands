"""Real verify.sh token wait behavior."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import cast, override

from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct import test_verify_step_routes as routes
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, Record, VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifyTokenWaitTests(VerifyTokenWaitFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = caller_admission(self, self.root, self.environment)

    def test_token_wait_restores_the_original_place_and_runs_with_memory_and_token(self) -> None:
        observer = self.root / "observe-token.py"
        observations = self.root / "token-observations.json"
        _ = observer.write_text('import sys\n' + f'sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})\n' + '''import json
import os
from pathlib import Path
import sys
from scripts.build_queue.admission_service import read_state

phase = sys.argv[1]
state = read_state(Path(os.environ["BUILDLOG_DIR"]) / "admission")
reservations = list(state.reservations.values())
lock = Path(os.environ["UNIT_SESSION_DIR"]) / "locks/cargo.d"
assert lock.is_dir(), phase
capture = Path(os.environ["TEST_TOKEN_OBSERVATIONS"])
rows = json.loads(capture.read_text()) if capture.exists() else {}
if phase == "admission":
    assert phase not in rows, rows
    assert not reservations, reservations
else:
    assert len(reservations) == 1, reservations
rows[phase] = {"holder": (lock / "holder_pid").read_text()}
capture.write_text(json.dumps(rows))
''')
        shell = self.root / "observe.sh"
        _ = shell.write_text('''set -T
observe_admission() {
    if [[ "${FUNCNAME[1]:-}" == buildlog_wait_for_memory && "$BASH_COMMAND" == 'with_token_renewal buildlog_wait_for_memory_only "$@"' ]]; then
        python3 "$TEST_TOKEN_OBSERVER" admission || exit 95
    fi
}
trap observe_admission DEBUG
''')
        result = self.verify({**self.environment, "BASH_ENV": str(shell),
                              "TEST_TOKEN_OBSERVER": str(observer), "TEST_TOKEN_OBSERVATIONS": str(observations)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        captured = cast(dict[str, Record], json.loads(observations.read_text()))
        self.assertEqual(set(captured), {"admission", "run"})
        self.assertEqual(captured["admission"]["holder"], captured["run"]["holder"])
        directory = self.root / "buildlog/admission"
        times = directory / "queue_times.jsonl"
        rows = [cast(Record, json.loads(line)) for line in times.read_text().splitlines()] if times.exists() else []
        self.assertEqual(sum(row.get("kind") == "token_gap" for row in rows), 0)
        self.assertEqual(list(directory.glob("*.reservation")), [])
        self.assertEqual(len(self.records()), 1)
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))

    def test_token_wait_names_a_holder_without_a_final_newline(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board)}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
            env={**environment, "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.started_groups.add(first.pid)
        self.wait_for_running(self.root / "running", first)
        output = self.root / "token-note.err"
        with output.open("wb") as stream:
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                env={**environment, "UNIT_TEAM_ROLE": "second"}, stdout=stream,
                stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(second.pid)
        self.wait_for_output(output, "waiting for the cargo token since", second)
        self.assertIn(f"first (pid {first.pid}) holds it", output.read_text())
        _ = (self.root / "release").touch()
        self.assertEqual(first.wait(timeout=TEST_HANG_GUARD_SECS), 0)
        self.assertEqual(second.wait(timeout=TEST_HANG_GUARD_SECS), 0, output.read_text())

    def test_mac_routing_under_hold_skips_initial_token_acquisition(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        _ = (holders / "other").write_text("fixture hold")
        targets = self.root / "targets.py"
        runner = self.root / "mac-runner.py"
        _ = targets.write_text(routes.TARGETS)
        _ = runner.write_text(routes.RUNNER)
        self.environment.update({
            "BUILD_TARGETS_SCRIPT": str(targets), "VERIFY_MAC_RUNNER": str(runner),
            "TEST_ROUTE_EVENTS": str(self.root / "route-events"),
            "TEST_MAC_EVENTS": str(self.root / "mac-events"),
            "MAC_TEST_CONFIG": str(self.root / "mac-settings"), "CONF_DIR": str(Path(self.root / "mac-settings").parent), "TEST_MAC_ALLOWED": "1",
        })
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [cast(Record, json.loads(line)) for line in (self.root / "mac-events").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(cast(Record, rows[0]["record"])["state"], "on_mac")
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertFalse((self.root / "session" / "locks" / "cargo.d").exists())
        self.assertEqual(self.records()[-1]["token_wait_s"], 0)

    def test_no_token_records_zero(self) -> None:
        # A session dir alone makes this a director's run, which takes the token.
        environment = {name: value for name, value in self.environment.items()
                       if name != "UNIT_SESSION_DIR"}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertEqual((self.root / "metadata-calls").read_text().splitlines(), ["metadata"])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))

    def test_director_run_takes_the_token_as_slot_director(self) -> None:
        clock = self.root / "director-clock"
        _ = clock.write_text("100\n")
        session = Path(self.environment["UNIT_SESSION_DIR"])
        result = self.verify({**self.environment, "UNIT_TOKEN_CLOCK_FILE": str(clock)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertIn("[director] claim: token cargo acquired", (session / "board.log").read_text())
        self.assertFalse((session / "locks" / "cargo.d").exists(), "the director's token was not released")

    def test_token_wait_uses_elapsed_seconds_when_no_clock_override_is_present(self) -> None:
        # Advance Bash's elapsed clock at the two reads, without the file
        # override. A real second passing is irrelevant to the subtraction.
        clock_reads = self.root / "elapsed-clock-reads"
        shell = self.root / "elapsed-clock.sh"
        _ = shell.write_text('''set -T
advance_elapsed_clock() {
    if [[ "$BASH_COMMAND" == 'TOKEN_CLOCK_NOW=$SECONDS' ]]; then
        if [[ -e "$TEST_CLOCK_READS" ]]; then SECONDS=120; else SECONDS=100; fi
        : > "$TEST_CLOCK_READS"
    fi
}
trap advance_elapsed_clock DEBUG
''')
        environment = {**self.environment, "BASH_ENV": str(shell),
                       "TEST_CLOCK_READS": str(clock_reads),
                       "UNIT_RUN_DIR": str(self.root / "clock-board"),
                       "UNIT_TEAM_ROLE": "clock"}
        for key in ("UNIT_WORKER_POLL_SECS",
                    "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
            _ = environment.pop(key, None)
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 20)



if __name__ == "__main__":
    _ = unittest.main()
