"""Record resource holders through private event pipes and real verify calls."""
from __future__ import annotations

from scripts.build_queue import admission_records

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import ClassVar, cast, override
import unittest

from scripts.direct.verify_token_wait_test_support import BOARD, VERIFY, VerifyTokenWaitFixture
from scripts.direct.verify_hold_wait_test_support import VerifyHoldWaitFixture
from scripts.direct.verify_mac_offload_test_support import ROUTING_WAIT_STUB
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB, SCRIPTS, TAPLO_STUB
from scripts.build_queue.admission_test_support import caller_admission
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file


FIRST_SESSION = fixture_runtime_id("thread-first")
NEXT_SESSION = fixture_runtime_id("thread-second")


def isolate_run_sources(environment: dict[str, str], root: Path) -> None:
    """Use private sources for the worker process that the fixture starts."""
    environment.update({"SCRATCH_ROOT": str(root / "sources/scratch"),
                        "ROSTER_RUN_ROOT": str(root / "sources/unit"),
                        "ASTRA_REVIEW_DIR": str(root / "sources/astra"),
                        "UNIT_DETACHED_RUN": "1"})


def wait_rows(session: Path) -> list[dict[str, object]]:
    """Read the real recorder's history through its private session state."""
    state = cast(dict[str, object], json.loads((session / "progress_history_state.json").read_text()))
    return [row for line in Path(cast(str, state["history_file"])).read_text().splitlines()
            if str((row := cast(dict[str, object], json.loads(line)))["event_type"]).startswith("wait_")]


def named_holder(kind: str, resource: str, owner: str, *, unit: bool = False) -> dict[str, object]:
    return {"state": "NamedHolder", "resource_kind": kind, "resource": resource,
            "holder": {"state": "UnitHolder", "unit": owner} if unit else
                      {"state": "SessionHolder", "session_id": owner}}


def recorded_holders(rows: list[dict[str, object]]) -> list[object]:
    return [row["holder"] for row in rows if row["event_type"] in ("wait_started", "wait_holder_seen")]


RECORDER_OBSERVER = '''import json, os, sys
from pathlib import Path
arguments = sys.argv[1:]
if arguments and arguments[0].endswith("progress_history.py"):
    with Path(os.environ["TEST_RECORDER_CALLS"]).open("a") as output:
        output.write(json.dumps(arguments[1:]) + "\\n")
    failure = Path(os.environ["TEST_RECORDER_FAILURE"])
    if arguments[1] == os.environ.get("TEST_FAIL_RECORDER") and not failure.exists():
        failure.touch()
        raise SystemExit(1)
os.execv(os.environ["TEST_REAL_PYTHON"], [os.environ["TEST_REAL_PYTHON"], *arguments])
'''

EVENT_CLIENT = '''import json, os, sys
from pathlib import Path
root = Path(os.environ["TEST_EVENT_ROOT"])
queue = json.loads((root / "episodes.json").read_text())
episode = queue.pop(0)
(root / "episodes.json").write_text(json.dumps(queue))
with (root / "client-calls.jsonl").open("a") as output:
    output.write(json.dumps({"since": sys.argv[sys.argv.index("--since") + 1],
                            "first": (root / "waits/first-queued-at").read_text()}) + "\\n")
for event in episode["events"]:
    print(event, flush=True)
Path(sys.argv[sys.argv.index("--shell-state") + 1]).write_text(
    "BUILDLOG_MEM_OUTCOME=" + episode["outcome"] + "\\n"
    + "BUILDLOG_MEM_WAIT_S=" + str(episode["seconds"]) + "\\nBUILDLOG_MEM_RESERVATION=''\\n")
raise SystemExit(75 if episode["outcome"] == "LeftQueue" else 0)
'''


class RecordedWaitFixture(VerifyTokenWaitFixture):
    """Seed private wait history once for each test class."""

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        isolate_run_sources(self.environment, self.root)
        self.record_phase(self.root / "session")


class VerifyMemoryWaitHolderTests(RecordedWaitFixture):
    event_errors: str = ""

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        session = self.root / "session"
        self.environment.update({"UNIT_RUN_DIR": str(session), "UNIT_TEAM_ROLE": "impl",
                                 "TEST_EVENT_ROOT": str(self.root), "TEST_REAL_PYTHON": sys.executable,
                                 "TEST_RECORDER_CALLS": str(self.root / "recorder-calls.jsonl"),
                                 "TEST_RECORDER_FAILURE": str(self.root / "recorder-failed")})
        waits = self.root / "waits"
        waits.mkdir()
        _ = (waits / "first-queued-at").write_text("100\n")
        observer = self.root / "recorder-python"
        _ = observer.write_text(f"#!{sys.executable}\n" + RECORDER_OBSERVER)
        observer.chmod(0o755)
        _ = (self.root / "event-client.py").write_text(EVENT_CLIENT)

    def run_events(self, episodes: list[dict[str, object]], *, failure: str = "") -> None:
        _ = (self.root / "episodes.json").write_text(json.dumps(episodes))
        self.environment["TEST_FAIL_RECORDER"] = failure
        script = self.root / "memory-events.sh"
        _ = script.write_text('set -euo pipefail\nsource "$1"\n'
            + shell_functions(VERIFY, "phase_wait_holder_flags", "phase_wait_start", "phase_wait_finish",
                              "phase_wait_finish_all", "buildlog_memory_event") + '''
PY=$TEST_EVENT_ROOT/recorder-python
PHASE_WAIT_RECORDER=$2
PHASE_WAIT_SESSION=$UNIT_RUN_DIR
HOLD_WAIT_DIR=$TEST_EVENT_ROOT/waits
BOARD_DIR=$UNIT_RUN_DIR
BUILDLOG_MEM_ADMIT_SCRIPT=$TEST_EVENT_ROOT/event-client.py
BUILDLOG_MEM_WAIT_SINCE=100
BUILDLOG_MEM_WAIT_FIRST_FILE=$HOLD_WAIT_DIR/first-queued-at
buildlog_memory_python() { "$PY" "$@"; }
for ((episode=0; episode<$3; episode++)); do
    buildlog_wait_for_memory cargo check
    printf '%s|%s\\n' "$BUILDLOG_MEM_OUTCOME" "$BUILDLOG_MEM_WAIT_S" >> "$TEST_EVENT_ROOT/outcomes"
done
''')
        result = subprocess.run(["bash", str(script), str(VERIFY.parents[1] / "lint/memory_gate.sh"),
                                 str(VERIFY.with_name("progress_history.py")), str(len(episodes))],
                                cwd=self.root, env=self.environment, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.event_errors = result.stderr

    def recorder_calls(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in
                (self.root / "recorder-calls.jsonl").read_text().splitlines()]

    def test_memory_pipe_records_session_unit_change_and_explicit_absence(self) -> None:
        self.run_events([{"events": ["first", 'holder ' + json.dumps({"kind": "session", "session_id": FIRST_SESSION,
                            "session_name": "render|unit"}),
                            'holder ' + json.dumps({"kind": "session", "session_id": NEXT_SESSION, "session_name": "next"}),
                            'holder {"kind":"unit","unit":"render-unit"}', 'holder {"kind":"none"}', "done"],
                          "outcome": "Granted", "seconds": 40}])
        rows = wait_rows(self.root / "session")
        holders = recorded_holders(rows)
        self.assertEqual(holders[1:4], [named_holder("memory", "memory", FIRST_SESSION),
                         named_holder("memory", "memory", NEXT_SESSION),
                         named_holder("memory", "memory", "render-unit", unit=True)])
        self.assertEqual(cast(dict[str, object], holders[-1])["state"], "HolderNotNamed")
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertEqual({row["wait_id"] for row in rows}, {started["wait_id"]})
        self.assertEqual(finished["wait_id"], started["wait_id"])

    def test_steady_memory_wait_creates_no_extra_recorder_calls(self) -> None:
        self.run_events([{"events": ["first", 'holder ' + json.dumps({"kind": "session", "session_id": FIRST_SESSION,
                            "session_name": "render"}), "done"], "outcome": "Granted", "seconds": 70}])
        rows = wait_rows(self.root / "session")
        self.assertEqual([row["event_type"] for row in rows], ["wait_started", "wait_holder_seen", "wait_finished"])
        self.assertEqual([call[0] for call in self.recorder_calls()], ["start-wait", "wait-holder", "finish-wait"])
        self.assertEqual(recorded_holders(rows)[-1], named_holder("memory", "memory", FIRST_SESSION))

    def test_recorder_failures_retry_without_changing_admission(self) -> None:
        for command in ("start-wait", "wait-holder", "not-recorded"):
            with self.subTest(command=command):
                (self.root / "recorder-failed").unlink(missing_ok=True)
                if command == "not-recorded":
                    self.environment["CODEX_THREAD_ID"] = fixture_runtime_id("unregistered-wait-worker")
                previous_rows = len(wait_rows(self.root / "session"))
                self.run_events([{"events": ["first", 'holder ' + json.dumps({"kind": "session",
                                "session_id": FIRST_SESSION, "session_name": "render"}),
                                'holder ' + json.dumps({"kind": "session", "session_id": NEXT_SESSION,
                                "session_name": "next"}), "done"], "outcome": "Granted", "seconds": 15}], failure=command)
                self.assertIn("wait history NotRecorded", self.event_errors)
                rows = wait_rows(self.root / "session")
                if command == "not-recorded":
                    self.assertEqual(self.event_errors.count("NotRecorded"), 1, self.event_errors)
                    self.assertNotIn("cannot update", self.event_errors)
                    self.assertEqual([row["event_type"] for row in rows[previous_rows:]], ["wait_not_recorded"])
                else:
                    self.assertEqual(recorded_holders(rows)[-1], named_holder("memory", "memory", NEXT_SESSION))
                    self.assertEqual(rows[-1]["event_type"], "wait_finished")
                self.assertEqual((self.root / "outcomes").read_text().splitlines()[-1], "Granted|15")

    def test_memory_rejoin_opens_new_intervals_and_keeps_queue_age(self) -> None:
        holder = 'holder ' + json.dumps({"kind": "session", "session_id": FIRST_SESSION, "session_name": "render"})
        self.run_events([{"events": ["first", holder, "left"], "outcome": "LeftQueue", "seconds": 30},
                         {"events": ["first", holder, "done"], "outcome": "Granted", "seconds": 55}])
        rows = wait_rows(self.root / "session")
        starts = [row for row in rows if row["event_type"] == "wait_started"]
        ends = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[0]["wait_id"], starts[1]["wait_id"])
        self.assertEqual([row["wait_id"] for row in ends], [row["wait_id"] for row in starts])
        self.assertLessEqual(cast(float, ends[0]["timestamp_epoch"]), cast(float, starts[1]["timestamp_epoch"]))
        clients = [cast(dict[str, str], json.loads(line)) for line in
                   (self.root / "client-calls.jsonl").read_text().splitlines()]
        self.assertEqual([row["since"] for row in clients], ["100", "100"])
        self.assertEqual([row["first"] for row in clients], ["100\n", "100\n"])
        self.assertEqual((self.root / "outcomes").read_text().splitlines(), ["LeftQueue|30", "Granted|55"])

    def test_failed_memory_close_retries_and_rejoin_starts_a_new_interval(self) -> None:
        first_holder = 'holder ' + json.dumps({"kind": "session", "session_id": FIRST_SESSION,
                                               "session_name": "first"})
        next_holder = 'holder ' + json.dumps({"kind": "session", "session_id": NEXT_SESSION,
                                              "session_name": "next"})
        self.run_events([{"events": ["first", first_holder, "left", "first", next_holder, "done"],
                          "outcome": "Granted", "seconds": 55}], failure="finish-wait")
        self.assertIn("wait history NotRecorded: cannot close", self.event_errors)
        rows = wait_rows(self.root / "session")
        starts = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual(len(starts), 2, rows)
        self.assertNotEqual(starts[0]["wait_id"], starts[1]["wait_id"])
        for start, holder in zip(starts, (FIRST_SESSION, NEXT_SESSION), strict=True):
            interval = [row for row in rows if row["wait_id"] == start["wait_id"]]
            self.assertEqual(recorded_holders(interval)[-1], named_holder("memory", "memory", holder))
            self.assertEqual(interval[-1]["event_type"], "wait_finished", interval)
        closes = [call[call.index("--wait-id") + 1] for call in self.recorder_calls()
                  if call[0] == "finish-wait"]
        self.assertEqual(closes.count(cast(str, starts[0]["wait_id"])), 2, closes)
        self.assertEqual(closes.count(cast(str, starts[1]["wait_id"])), 1, closes)
        self.assertEqual((self.root / "outcomes").read_text(), "Granted|55\n")

    def test_immediate_admission_records_no_wait_or_holder_call(self) -> None:
        self.run_events([{"events": [], "outcome": "Granted", "seconds": 0}])
        self.assertEqual(wait_rows(self.root / "session"), [])
        calls = self.root / "recorder-calls.jsonl"
        self.assertEqual(calls.read_text() if calls.exists() else "", "")
        self.assertEqual((self.root / "outcomes").read_text(), "Granted|0\n")


class VerifyTokenHolderTests(VerifyTokenWaitFixture):
    def test_token_wait_names_the_owner_and_updates_it_on_the_next_pass(self) -> None:
        isolate_run_sources(self.environment, self.root)
        board = self.root / "board"
        self.record_phase(board)
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("The fixture needs sleep.")
        environment = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl",
                       "TEST_POLL_ROOT": str(self.root), "BUILDLOG_MEM_POLL_S": "1",
                       "TEST_REAL_SLEEP": real_sleep}
        lease = subprocess.run(["bash", str(BOARD), "acquire", str(board), "fixture-first", "cargo",
                                "--pid", str(os.getpid()), "--hold", "3600"],
                               env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(lease.returncode, 0, lease.stderr)
        self.write_wait_sleep('''read -r caller < "$TEST_POLL_ROOT/caller-pid"
if [ "$PPID" != "$caller" ]; then exec "$TEST_REAL_SLEEP" "$@"; fi
if [ "$1" = 1 ] || [ "$1" = 3 ]; then
    count=0
    [ ! -f "$TEST_POLL_ROOT/passes" ] || read -r count < "$TEST_POLL_ROOT/passes"
    count=$((count + 1))
    printf '%s\\n' "$count" > "$TEST_POLL_ROOT/passes"
    touch "$TEST_POLL_ROOT/poll-$count"
    exec python3 "$TEST_WAIT_HELPER" "$TEST_POLL_ROOT/resume-$count"
fi
exit 0
''')
        process = self.start_check(environment)
        self.addCleanup(process.communicate, timeout=TEST_HANG_GUARD_SECS)
        _ = (self.root / "caller-pid").write_text(str(process.pid) + "\n")
        self.wait_for_running(self.root / "poll-1", process)
        self.assertEqual(recorded_holders(wait_rows(board)), [named_holder("slot", "cargo", FIRST_SESSION)])
        for verb, owner in (("release", "fixture-first"), ("acquire", "fixture-second")):
            changed = subprocess.run(["bash", str(BOARD), verb, str(board), owner, "cargo",
                                      "--pid", str(os.getpid()), *(["--hold", "3600"] if verb == "acquire" else [])],
                                     env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(changed.returncode, 0, changed.stderr)
        (self.root / "resume-1").touch()
        self.wait_for_running(self.root / "poll-2", process)
        self.assertEqual(recorded_holders(wait_rows(board)), [named_holder("slot", "cargo", FIRST_SESSION),
                                                              named_holder("slot", "cargo", NEXT_SESSION)])
        released = subprocess.run(["bash", str(BOARD), "release", str(board), "fixture-second", "cargo",
                                    "--pid", str(os.getpid())], env=environment,
                                   capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(released.returncode, 0, released.stderr)
        (self.root / "resume-2").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(wait_rows(board)[-1]["event_type"], "wait_finished")


class VerifyRecordedTokenIdentityTests(RecordedWaitFixture):
    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        board = self.root / "token-board"
        board.mkdir()
        with isolated_staff_file(self.root / "staff.json", self.environment):
            owner, = staff.initial_snapshot().find(kind="unit")
        self.write_token_record("progress_history_state.json", {"owner_staff_id": owner.id})
        waits = self.root / "identity-waits"
        waits.mkdir()
        observer = self.root / "identity-python"
        _ = observer.write_text('''#!/bin/sh
printf '%s\\n' "${VERIFY_PY_HELPER:-recorder}" >> "$TEST_HOLDER_PY_CALLS"
exec "$TEST_REAL_PYTHON" "$@"
''')
        observer.chmod(0o755)
        self.environment.update({"TEST_HOLDER_PY_CALLS": str(self.root / "identity-python-calls"),
                                 "TEST_REAL_PYTHON": sys.executable,
                                 "TEST_IDENTITY_ROOT": str(self.root)})

    def write_token_record(self, name: str, record: dict[str, object]) -> None:
        _ = (self.root / "token-board" / name).write_text(json.dumps(record))

    def worker_reference(self, name: str) -> str:
        roster = cast(dict[str, str], json.loads(
            (self.root / f"session/impl_worker_{name}.json").read_text()))
        return roster["staff_id"]

    def acquire_identity_token(self, owner: str) -> None:
        result = subprocess.run(["bash", str(BOARD), "acquire", str(self.root / "token-board"),
                                 owner, "cargo", "--pid", str(os.getpid()), "--hold", "3600"],
                                env=self.environment, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def record_token_passes(self, body: str = 'phase_wait_start token "cargo token"\n') -> list[dict[str, object]]:
        recorded_count = len(wait_rows(self.root / "session"))
        script = self.root / "identity-wait.sh"
        _ = script.write_text('set -euo pipefail\n' + shell_functions(
            VERIFY, "phase_wait_holder_flags", "phase_wait_start", "phase_wait_finish") + '''
PY=$TEST_IDENTITY_ROOT/identity-python
PHASE_WAIT_SESSION=$TEST_IDENTITY_ROOT/session
PHASE_WAIT_RECORDER=$1
HOLD_WAIT_DIR=$TEST_IDENTITY_ROOT/identity-waits
BOARD_DIR=$TEST_IDENTITY_ROOT/token-board
''' + body + '\nphase_wait_finish token\n')
        result = subprocess.run(["bash", str(script), str(VERIFY.with_name("progress_history.py"))],
                                cwd=self.root, env=self.environment, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = wait_rows(self.root / "session")[recorded_count:]
        self.assertEqual(rows[0]["event_type"], "wait_started", rows)
        self.assertEqual(rows[-1]["event_type"], "wait_finished", rows)
        self.assertEqual({row["wait_id"] for row in rows}, {rows[0]["wait_id"]})
        return rows

    def test_director_token_uses_the_recorded_owner_without_a_director_file(self) -> None:
        self.acquire_identity_token("director")
        rows = self.record_token_passes('phase_wait_start token "cargo token"\n' * 3)
        self.assertEqual(recorded_holders(rows), [named_holder(
            "slot", "cargo", fixture_runtime_id("fixture-director"))])
        self.assertEqual((self.root / "identity-python-calls").read_text().splitlines(),
                         ["PHASE_WAIT_HOLDER", "recorder", "recorder"])

    def test_saved_worker_record_names_the_session_without_a_roster(self) -> None:
        reference = self.worker_reference("fixture-first")
        self.write_token_record("impl_worker_saved.json", {"slot": "writer", "staff_id": reference,
                                                          "model": "fixture"})
        self.acquire_identity_token("writer")
        self.assertEqual(recorded_holders(self.record_token_passes()),
                         [named_holder("slot", "cargo", FIRST_SESSION)])

    def test_dispatch_pass_names_the_worker_session_without_worker_files(self) -> None:
        state = cast(dict[str, object], json.loads(
            (self.root / "token-board/progress_history_state.json").read_text()))
        state["pass"] = {
            "impl": {"worker_name": "impl", "worker_id": self.worker_reference("fixture-first")}}
        self.write_token_record("progress_history_state.json", state)
        self.acquire_identity_token("impl")
        self.assertEqual(recorded_holders(self.record_token_passes()),
                         [named_holder("slot", "cargo", FIRST_SESSION)])

    def test_dispatch_worker_name_resolves_when_the_dispatch_slot_differs(self) -> None:
        state = cast(dict[str, object], json.loads(
            (self.root / "token-board/progress_history_state.json").read_text()))
        state["pass"] = {
            "impl": {"worker_name": "impl2", "worker_id": self.worker_reference("fixture-first")}}
        self.write_token_record("progress_history_state.json", state)
        self.acquire_identity_token("impl2")
        self.assertEqual(recorded_holders(self.record_token_passes()),
                         [named_holder("slot", "cargo", FIRST_SESSION)])

    def test_duplicate_dispatch_worker_names_use_the_owner_unit(self) -> None:
        state = cast(dict[str, object], json.loads(
            (self.root / "token-board/progress_history_state.json").read_text()))
        state["pass"] = {
            "impl": {"worker_name": "impl2", "worker_id": self.worker_reference("fixture-first")},
            "test": {"worker_name": "impl2", "worker_id": self.worker_reference("fixture-second")}}
        self.write_token_record("progress_history_state.json", state)
        self.acquire_identity_token("impl2")
        self.assertEqual(recorded_holders(self.record_token_passes()),
                         [named_holder("slot", "cargo", "wait", unit=True)])

    def test_saved_worker_stem_wins_over_a_roster_slot_match(self) -> None:
        self.write_token_record("impl_worker_writer.json", {
            "staff_id": self.worker_reference("fixture-first")})
        self.acquire_identity_token("writer")
        self.assertEqual(recorded_holders(self.record_token_passes()),
                         [named_holder("slot", "cargo", FIRST_SESSION)])

    def test_missing_worker_staff_row_uses_the_recorded_owner_unit(self) -> None:
        self.write_token_record("impl_worker_writer.json", {"slot": "writer", "staff_id": "missing-worker"})
        self.acquire_identity_token("writer")
        self.assertEqual(recorded_holders(self.record_token_passes()),
                         [named_holder("slot", "cargo", "wait", unit=True)])

    def test_ambiguous_worker_records_do_not_choose_a_session(self) -> None:
        self.write_token_record("impl_worker_first.json", {
            "slot": "writer", "staff_id": self.worker_reference("fixture-first")})
        self.write_token_record("impl_worker_second.json", {
            "slot": "writer", "staff_id": self.worker_reference("fixture-second")})
        self.acquire_identity_token("writer")
        self.assertEqual(recorded_holders(self.record_token_passes()),
                         [named_holder("slot", "cargo", "wait", unit=True)])

    def test_occupied_slot_retries_until_a_saved_identity_appears(self) -> None:
        self.write_token_record("progress_history_state.json", {})
        _ = (self.root / "pending-worker.json").write_text(json.dumps({
            "slot": "writer", "staff_id": self.worker_reference("fixture-first")}))
        self.acquire_identity_token("writer")
        rows = self.record_token_passes('''phase_wait_start token "cargo token"
cp "$TEST_IDENTITY_ROOT/pending-worker.json" "$BOARD_DIR/impl_worker_writer.json"
phase_wait_start token "cargo token"
phase_wait_start token "cargo token"
''')
        holders = recorded_holders(rows)
        self.assertEqual(cast(dict[str, object], holders[0])["state"], "HolderNotNamed")
        self.assertEqual(holders[1:], [named_holder("slot", "cargo", FIRST_SESSION)])
        self.assertEqual((self.root / "identity-python-calls").read_text().splitlines().count(
            "PHASE_WAIT_HOLDER"), 2)

    def test_absent_and_unknown_token_owners_do_not_start_a_lookup(self) -> None:
        for owner in ("", "unknown"):
            with self.subTest(owner=owner):
                if owner:
                    self.acquire_identity_token(owner)
                calls = self.root / "identity-python-calls"
                calls.unlink(missing_ok=True)
                rows = self.record_token_passes('phase_wait_start token "cargo token"\n' * 3)
                self.assertEqual(cast(dict[str, object], recorded_holders(rows)[-1])["state"], "HolderNotNamed")
                self.assertEqual(calls.read_text().splitlines(), ["recorder", "recorder"])


class VerifyHoldHolderTests(VerifyHoldWaitFixture):
    def test_local_build_hold_names_its_holder_and_closes_before_the_step(self) -> None:
        isolate_run_sources(self.fixture.environment, self.fixture.root)
        self.set_hold_routing("natedev_only")
        self.hold(FIRST_SESSION)
        _ = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        process = self.start("check", "sample", "--local", "holder test")
        self.fixture.wait_until(lambda: bool(wait_rows(session)), "the recorded hold", (process,), directory=session)
        self.assertEqual(recorded_holders(wait_rows(session)), [named_holder("slot", "natedev", FIRST_SESSION)])
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / FIRST_SESSION).unlink()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = wait_rows(session)
        self.assertEqual(rows[-1]["event_type"], "wait_finished")
        self.assertEqual(rows[0]["wait_id"], rows[-1]["wait_id"])
        self.assertTrue(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))


CAPACITY_TOOL = '''import os, runpy, sys
from pathlib import Path
root = Path(os.environ["PACKAGE_ROOT"])
if sys.argv[1] == "eligible" and (root / "target-starting").exists():
    print("any_target")
    print("mac\\texcluded\\tstarting\\t" + os.environ["MAC_TEST_CONFIG"] + "\\tmacos")
    raise SystemExit(0)
runpy.run_path(os.environ["TEST_REAL_TARGETS"], run_name="__main__")
'''

POLL_SLEEP = '''#!/bin/sh
if [ "$1" != "${BUILD_TARGETS_POLL_S}" ]; then exec "$TEST_REAL_SLEEP" "$@"; fi
count=0
[ ! -f "$PACKAGE_ROOT/target-passes" ] || read -r count < "$PACKAGE_ROOT/target-passes"
count=$((count + 1))
printf '%s\\n' "$count" > "$PACKAGE_ROOT/target-passes"
touch "$PACKAGE_ROOT/target-poll-$count"
exec "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$PACKAGE_ROOT/target-resume-$count"
'''

RUN_HISTORY = '''
session = Path(os.environ["UNIT_RUN_DIR"])
state = json.loads((session / "progress_history_state.json").read_text())
rows = [json.loads(line) for line in Path(state["history_file"]).read_text().splitlines()]
waits = [row for row in rows if row["event_type"].startswith("wait_")]
with (Path(os.environ["PACKAGE_ROOT"]) / "run-waits.jsonl").open("a") as output:
    output.write(json.dumps({"args": arguments, "waits": waits}) + "\\n")
'''


RESERVATION_RACE_TOOL = '''import os, runpy, sys
from pathlib import Path
root = Path(os.environ["PACKAGE_ROOT"])
if sys.argv[1] == "reserve" and not (root / "reservation-raced").exists():
    if os.environ["TEST_RACE_WITH_CLAIM"] == "0":
        raise SystemExit(1)
    with open(Path(os.environ["TEST_REAL_TARGETS"]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.mac_test import mac_test
    paths = mac_test.state_paths("linux-box")
    scope = mac_test.target_claim_scope(paths, str(root))
    claim = mac_test.claim_path(paths, scope)
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(os.environ["TEST_RACE_CLAIM_JSON"])
    (root / "reservation-raced").write_text(str(claim))
try:
    runpy.run_path(os.environ["TEST_REAL_TARGETS"], run_name="__main__")
except SystemExit as result:
    if sys.argv[1] == "reserve":
        with (root / "reservation-results").open("a") as output:
            output.write(str(result.code) + "\\n")
    raise
'''


class VerifyTargetWaitHolderTests(VerifyHoldWaitFixture):
    recorded_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        seed = cls()
        fixture = seed.fixture
        fixture.root = fixture.fixture_root
        fixture.environment = fixture.fixture_environment.copy()
        _ = shutil.copytree(fixture.fixture_seed, fixture.root, symlinks=True)
        try:
            isolate_run_sources(fixture.environment, fixture.root)
            _ = seed.record_phase()
            cls.recorded_environment = {key: value for key, value in fixture.environment.items()
                                        if fixture.fixture_environment.get(key) != value}
            shutil.rmtree(fixture.fixture_seed)
            _ = shutil.copytree(fixture.root, fixture.fixture_seed, symlinks=True)
        finally:
            shutil.rmtree(fixture.root)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.fixture.environment.update(self.recorded_environment)

    def prepare_target_wait(self) -> Path:
        root = self.fixture.root
        claims = Path(self.fixture.environment["MAC_TEST_STATE_DIR"]) / "claims"
        claims.mkdir(parents=True, exist_ok=True)
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("The fixture needs sleep.")
        tool = root / "capacity-tool.py"
        _ = tool.write_text(CAPACITY_TOOL)
        self.fixture.write_executable(self.fixture.bin_directory / "sleep", POLL_SLEEP)
        local_wait = root / "target-local-wait.py"
        _ = local_wait.write_text(ROUTING_WAIT_STUB)
        self.fixture.environment.update({"BUILD_TARGETS_SCRIPT": str(VERIFY.parents[1] / "build_targets/build_targets.py"),
            "TEST_REAL_TARGETS": str(VERIFY.parents[1] / "build_targets/build_targets.py"),
            "TEST_REAL_SLEEP": real_sleep, "VERIFY_LOCAL_RELEASE_WAIT_SCRIPT": str(local_wait),
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        git = self.fixture.bin_directory / "git"
        source = git.read_text().replace("wanted=rev-parse\n", '''if [[ "$*" == "rev-parse HEAD" ]]; then
    printf '%040d\\n' 1
    exit 0
fi
wanted=rev-parse
''')
        self.fixture.write_executable(git, source)
        runner = self.fixture.runner
        source = runner.read_text().replace('raise SystemExit(runner_status)', RUN_HISTORY + '''
if os.environ.get("TEST_REHOLD_FINAL") == "1" and attempt == 1:
    Path(os.environ["TEST_CLAIM_FILE"]).write_text(os.environ["TEST_NEXT_CLAIM"])
raise SystemExit(runner_status)
''')
        self.fixture.write_executable(runner, source)
        return claims / f"{root.name}.json"

    def claim(self, path: Path, owner: str) -> dict[str, object]:
        row: dict[str, object] = {"claim_id": f"claim-{owner}", "episode": f"episode-{owner}",
            "attempt": "fixture-attempt", "submitting_session": owner,
            "pid": os.getpid(), "proc_start": admission_records.process_start_time(os.getpid()),
            "what": "private fixture work", "worktree": str(self.fixture.root),
            "since": datetime.now(timezone.utc).isoformat(), "holder_session": owner}
        _ = path.write_text(json.dumps(row))
        return row

    def pause(self, process: subprocess.Popen[str], number: int) -> list[dict[str, object]]:
        self.fixture.wait_for_path(self.fixture.root / f"target-poll-{number}", (process,))
        return wait_rows(Path(self.fixture.environment["UNIT_RUN_DIR"]))

    def resume(self, number: int) -> None:
        (self.fixture.root / f"target-resume-{number}").touch()

    def assert_closed_before_remote_steps(self) -> list[dict[str, object]]:
        snapshots = [cast(dict[str, object], json.loads(line)) for line in
                     (self.fixture.root / "run-waits.jsonl").read_text().splitlines()]
        for snapshot in snapshots:
            rows = cast(list[dict[str, object]], snapshot["waits"])
            started = {cast(str, row["wait_id"]) for row in rows if row["event_type"] == "wait_started"}
            finished = {cast(str, row["wait_id"]) for row in rows if row["event_type"] == "wait_finished"}
            self.assertTrue(started)
            self.assertEqual(started, finished, snapshot)
        return snapshots

    def test_token_wait_closes_before_a_reserved_remote_test_starts(self) -> None:
        _ = self.prepare_target_wait()
        config = self.fixture.root / "build_targets.conf"
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        self.fixture.environment["TEST_TOKEN_RECORD"] = str(session / "locks/cargo.d/owned")
        runner = self.fixture.runner
        source = runner.read_text().replace(RUN_HISTORY, "")
        self.fixture.write_executable(runner, source.replace("event = {", RUN_HISTORY + "event = {", 1))
        acquired = subprocess.run(["bash", str(BOARD), "acquire", str(session), "fixture-impl", "cargo",
                                   "--pid", str(os.getpid()), "--hold", "3600"],
                                  env=self.fixture.environment, capture_output=True, text=True,
                                  timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(acquired.returncode, 0, acquired.stdout + acquired.stderr)
        process = self.start("test", "sample", "--no-cache")
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        snapshots = self.assert_closed_before_remote_steps()
        snapshot, = snapshots
        rows = cast(list[dict[str, object]], snapshot["waits"])
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual(started["wait_reason"], "cargo token")
        self.assertEqual(recorded_holders(rows),
                         [named_holder("slot", "cargo", fixture_runtime_id("thread-impl"))])
        self.assertEqual(self.fixture.local_runs(), [])

    def test_pinned_linux_reservation_race_waits_only_with_a_live_claim(self) -> None:
        _ = self.prepare_target_wait()
        root = self.fixture.root
        settings = root / "linux-box.conf"
        _ = write_config_fixture(settings, "offload=on\nhost=fake-linux\nkinds=test,check\nslots=1\n",
                                 template="aws_target")
        _ = (root / "build_targets.conf").write_text(
            "targets=linux-box,natedev\nkind.linux-box=ssh\nplatform.linux-box=linux\n"
            + "settings.linux-box=linux-box.conf\nkind.natedev=local\nplatform.natedev=linux\n")
        wrapper = root / "reservation-race.py"
        _ = wrapper.write_text(RESERVATION_RACE_TOOL)
        owner = self.claim(root / "race-owner.json", NEXT_SESSION)
        self.fixture.environment.update({"BUILD_TARGETS_SCRIPT": str(wrapper),
                                         "TEST_RACE_CLAIM_JSON": json.dumps(owner)})
        with self.subTest(reservation="no live claim"):
            self.fixture.environment["TEST_RACE_WITH_CLAIM"] = "0"
            refused = self.start("check", "sample", "--target", "linux-box", "--no-cache")
            code, output = self.finish(refused)
            self.assertEqual(code, 2, output)
            self.assertIn("target linux-box refused: admission changed", output)
            self.assertEqual(wait_rows(Path(self.fixture.environment["UNIT_RUN_DIR"])), [])
            self.assertEqual(self.fixture.runner_events(), [])
        with self.subTest(reservation="live claim"):
            self.fixture.environment["TEST_RACE_WITH_CLAIM"] = "1"
            process = self.start("check", "sample", "--target", "linux-box", "--no-cache")
            rows = self.pause(process, 1)
            self.assertEqual(recorded_holders(rows), [named_holder("slot", "linux-box", NEXT_SESSION)])
            self.assertEqual((root / "reservation-results").read_text().splitlines(), ["1"])
            self.assertEqual(self.fixture.runner_events(), [])
            claim = Path((root / "reservation-raced").read_text())
            claim.unlink()
            self.resume(1)
            code, output = self.finish(process)
            self.assertEqual(code, 0, output)
            snapshots = self.assert_closed_before_remote_steps()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(self.fixture.local_runs(), [])

    def test_pinned_check_names_a_busy_claim_and_its_next_owner(self) -> None:
        claim = self.prepare_target_wait()
        _ = self.claim(claim, FIRST_SESSION)
        process = self.start("check", "sample", "--target", "mac", "--no-cache")
        rows = self.pause(process, 1)
        self.assertEqual(recorded_holders(rows), [named_holder("slot", "mac", FIRST_SESSION)])
        _ = self.claim(claim, NEXT_SESSION)
        self.resume(1)
        rows = self.pause(process, 2)
        self.assertEqual(recorded_holders(rows), [named_holder("slot", "mac", FIRST_SESSION),
                                                 named_holder("slot", "mac", NEXT_SESSION)])
        claim.unlink()
        self.resume(2)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        snapshots = self.assert_closed_before_remote_steps()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(self.fixture.local_runs(), [])

    def test_pinned_final_names_both_busy_intervals_and_closes_each_before_running(self) -> None:
        claim = self.prepare_target_wait()
        next_claim = self.claim(claim, NEXT_SESSION)
        _ = self.claim(claim, FIRST_SESSION)
        self.fixture.environment.update({"TEST_REHOLD_FINAL": "1", "TEST_CLAIM_FILE": str(claim),
                                         "TEST_NEXT_CLAIM": json.dumps(next_claim)})
        process = self.start("final", "--target", "mac", "--no-cache")
        rows = self.pause(process, 1)
        self.assertEqual(recorded_holders(rows)[-1], named_holder("slot", "mac", FIRST_SESSION))
        claim.unlink()
        self.resume(1)
        rows = self.pause(process, 2)
        self.assertEqual(recorded_holders(rows)[-1], named_holder("slot", "mac", NEXT_SESSION))
        claim.unlink()
        self.resume(2)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = wait_rows(Path(self.fixture.environment["UNIT_RUN_DIR"]))
        self.assertEqual(recorded_holders(rows), [named_holder("slot", "mac", FIRST_SESSION),
                                                 named_holder("slot", "mac", NEXT_SESSION)])
        snapshots = self.assert_closed_before_remote_steps()
        self.assertEqual(len(snapshots), 2)
        self.assertEqual([sum(row["event_type"] == "wait_finished" for row in
                              cast(list[dict[str, object]], snapshot["waits"])) for snapshot in snapshots], [1, 2])

    def test_pinned_starting_target_records_an_unnamed_holder(self) -> None:
        _ = self.prepare_target_wait()
        self.fixture.environment["BUILD_TARGETS_SCRIPT"] = str(self.fixture.root / "capacity-tool.py")
        starting = self.fixture.root / "target-starting"
        starting.touch()
        process = self.start("check", "sample", "--target", "mac", "--no-cache")
        rows = self.pause(process, 1)
        holder, = recorded_holders(rows)
        self.assertEqual(cast(dict[str, object], holder)["state"], "HolderNotNamed")
        starting.unlink()
        self.resume(1)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        _ = self.assert_closed_before_remote_steps()

    def test_mac_only_wait_names_each_claim_owner(self) -> None:
        claim = self.prepare_target_wait()
        _ = self.claim(claim, FIRST_SESSION)
        free = self.fixture.root / "mac-free"
        self.fixture.environment.update({"TEST_RUNNER_BUSY_UNTIL": str(free), "TEST_RUNNER_BUSY_REASON": "busy"})
        process = self.start("test", "sample", "--mac", "holder test", "--no-cache")
        rows = self.pause(process, 1)
        self.assertIn(named_holder("slot", "mac", FIRST_SESSION), recorded_holders(rows))
        _ = self.claim(claim, NEXT_SESSION)
        self.resume(1)
        rows = self.pause(process, 2)
        self.assertEqual(recorded_holders(rows)[-1], named_holder("slot", "mac", NEXT_SESSION))
        claim.unlink()
        free.touch()
        self.resume(2)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        _ = self.assert_closed_before_remote_steps()

    def test_routing_hold_names_the_new_holder_on_the_next_pass(self) -> None:
        claim = self.prepare_target_wait()
        self.hold(FIRST_SESSION)
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        _ = self.claim(claim, FIRST_SESSION)
        self.fixture.environment["TEST_RUNNER_BUSY_UNTIL"] = str(self.fixture.root / "mac-free")
        process = self.start("test", "sample", "--no-cache")
        rows = self.pause(process, 1)
        self.assertEqual(recorded_holders(rows)[-1], named_holder("slot", "natedev", FIRST_SESSION))
        os.rename(holders / FIRST_SESSION, holders / NEXT_SESSION)
        self.resume(1)
        rows = self.pause(process, 2)
        self.assertEqual(recorded_holders(rows)[-1], named_holder("slot", "natedev", NEXT_SESSION))
        (holders / NEXT_SESSION).unlink()
        self.resume(2)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertEqual(wait_rows(Path(self.fixture.environment["UNIT_RUN_DIR"]))[-1]["event_type"], "wait_finished")
        self.assertEqual(len(self.fixture.local_runs()), 1)


MEMORY_CLIENT_OBSERVER = '''import json, os, sys
from pathlib import Path
arguments = sys.argv[1:]
if arguments and arguments[0].endswith("admission_client.py") and arguments[1] == "admit":
    with (Path(os.environ["PACKAGE_ROOT"]) / "memory-joins.jsonl").open("a") as output:
        output.write(json.dumps({"since": arguments[arguments.index("--since") + 1],
            "first": Path(os.environ["BUILDLOG_MEM_WAIT_FIRST_FILE"]).read_text()}) + "\\n")
os.execv(os.environ["TEST_REAL_PYTHON"], [os.environ["TEST_REAL_PYTHON"], *arguments])
'''


class VerifyMemoryRouteHolderTests(VerifyHoldWaitFixture):
    def prepare_memory_route(self) -> Path:
        isolate_run_sources(self.fixture.environment, self.fixture.root)
        _ = self.record_phase()
        observer = self.fixture.root / "observe-memory-python"
        recorder = self.fixture.root / "observe-memory-client.py"
        self.fixture.write_executable(recorder, MEMORY_CLIENT_OBSERVER)
        self.fixture.write_executable(observer, '''#!/bin/sh
case "$1" in
    */admission_client.py)
        if [ "$2" = admit ]; then
            exec "$TEST_REAL_PYTHON" "$TEST_MEMORY_OBSERVER" "$@"
        fi
        ;;
esac
exec "$TEST_REAL_PYTHON" "$@"
''')
        self.fixture.environment["TEST_MEMORY_OBSERVER"] = str(recorder)
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        _ = self.fixture.hold_memory()
        return Path(self.fixture.environment["UNIT_RUN_DIR"])

    def release_memory(self) -> None:
        admission = caller_admission(self.fixture, self.fixture.root, self.fixture.environment)
        with admission.loop_turn():
            admission.free(64 * 1024**3)

    def assert_closed_memory_intervals(self, session: Path) -> None:
        rows = [row for row in wait_rows(session) if row.get("wait_reason") == "memory admission"]
        starts = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual(len(starts), 2, rows)
        self.assertNotEqual(starts[0]["wait_id"], starts[1]["wait_id"])
        for start in starts:
            interval = [row for row in rows if row["wait_id"] == start["wait_id"]]
            self.assertEqual(interval[-1]["event_type"], "wait_finished", interval)
            named = [cast(dict[str, object], holder) for holder in recorded_holders(interval)
                     if cast(dict[str, object], holder)["state"] == "NamedHolder"]
            self.assertTrue(named, interval)
            self.assertTrue(all(holder["resource_kind"] == "memory" and holder["resource"] == "memory"
                                for holder in named), named)
        joins = [cast(dict[str, str], json.loads(line)) for line in
                 (self.fixture.root / "memory-joins.jsonl").read_text().splitlines()]
        self.assertGreaterEqual(len(joins), 2)
        self.assertEqual({join["since"] for join in joins}, {joins[0]["since"]})
        self.assertEqual({join["first"].strip() for join in joins}, {joins[0]["since"]})

    def test_refused_mac_claim_rejoins_with_a_new_interval_and_the_same_queue_age(self) -> None:
        session = self.prepare_memory_route()
        self.fixture.environment.update({"TEST_RUNNER_BUSY_UNTIL": str(self.fixture.runner_state / "mac-free"),
                                         **self.fixture.leave_once()})
        process = self.start("test", "sample", "--no-cache")
        self.fixture.wait_for_path(self.fixture.runner_state / "free-now-after-leave", (process,))
        self.fixture.wait_until(lambda: sum(row["event_type"] == "wait_started" for row in wait_rows(session)) == 2,
                                "the second memory interval", (process,), directory=session)
        self.assertEqual(len(self.fixture.runner_events()), 2)
        self.assertEqual(self.fixture.local_runs(), [])
        self.release_memory()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assert_closed_memory_intervals(session)
        self.assertEqual(len(self.fixture.local_runs()), 1)

    def test_first_mend_leaves_for_a_mac_finding_then_records_the_local_repair_wait(self) -> None:
        session = self.prepare_memory_route()
        self.fixture.write_executable(self.fixture.bin_directory / "cargo", CARGO_STUB)
        self.fixture.write_executable(self.fixture.bin_directory / "taplo", TAPLO_STUB)
        config = self.fixture.root / "lint.conf"
        _ = write_config_fixture(config, "mend=on\nfmt=on\nclippy=on\n")
        self.fixture.environment.update({"LINT_CONFIG_READER": str(SCRIPTS / "lint/lint_config.sh"),
            "LINT_CONFIG_FILE": str(config), "LINT_SKIP_SWEEP": "1",
            "TEST_TAPLO_EVENTS": str(self.fixture.root / "taplo-events.jsonl"), "TEST_TAPLO_STATUS": "0",
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "101"})
        process = self.start("lint", "sample")
        self.fixture.wait_until(lambda: len(self.fixture.runner_events()) == 2, "both Mac judgments", (process,),
                                directory=self.fixture.runner_state)
        self.fixture.wait_until(lambda: sum(row["event_type"] == "wait_started" for row in wait_rows(session)) == 2,
                                "the local repair interval", (process,), directory=session)
        self.assertFalse(any(row["args"][:1] == ["mend"] and "--fix" in row["args"]
                             for row in self.fixture.cargo_events()))
        self.fixture.set_mac_answer("passed", "", 0)
        self.release_memory()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assert_closed_memory_intervals(session)
        self.assertTrue(any(row["args"][:1] == ["mend"] and "--fix" in row["args"]
                            for row in self.fixture.cargo_events()))
        arguments = [row["args"] for row in self.fixture.runner_events()]
        self.assertEqual([words[words.index("--verb") + 1] for words in arguments],
                         ["mend-check", "clippy", "clippy"])


if __name__ == "__main__":
    _ = unittest.main()
