#!/usr/bin/env python3
"""Exercise the Mac copy watcher with a stand-in natedev step and runner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict, cast, final, override

sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ..lib import process_start
    from ..lib.process_start_test_support import write_stat
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start
    from scripts.lib.process_start_test_support import write_stat

import copy_watch
import mac_test
import offload


SCRIPT = Path(__file__).with_name("copy_watch.py")
HANG_GUARD_S = 60.0
# Ticks of the watcher at BUILD_TARGETS_POLL_S=0.05, enough for it to act on a
# change it would act on at all.
SETTLE_S = 0.5


class ClaimRecord(TypedDict):
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str


# The natedev step: prints the run line, then a cargo line once
# `compile` exists, a nextest Starting line once `tests` exists, and ends with
# 0 once `finish` exists. SIGTERM ends it with 143 after writing `terminated`.
STEP_STUB = r'''
import os
from pathlib import Path
import signal
import sys
import time

root = Path(sys.argv[1])
log = Path(sys.argv[2])

def stop(signal_number, frame):
    (root / "terminated").write_text("terminated\n")
    raise SystemExit(143)

signal.signal(signal.SIGTERM, stop)
with log.open("a") as output:
    output.write("+ cargo nextest run --workspace\n")
    output.flush()
    compiled = tested = False
    while not (root / "finish").exists():
        if not compiled and (root / "compile").exists():
            output.write("\x1b[1m\x1b[32m   Compiling\x1b[0m sample v0.1.0\n")
            output.flush()
            compiled = True
        if not tested and (root / "tests").exists():
            output.write("\x1b[1m\x1b[32m    Starting\x1b[0m 1 test across 1 binary\n")
            output.flush()
            tested = True
        time.sleep(0.01)
'''


# offload.py run as the watcher starts it. Each start appends its arguments to
# runner-events.jsonl and takes the next word of TEST_COPY_MODES (the last one
# repeats): failed, passed, lost, declined:<reason>, gave_way (exit 143 with no
# result), hold (wait for SIGTERM, then exit 143 after writing runner-stopped)
# or wait_failed (fail once `release-runner` exists). A `+busy` suffix first
# writes TEST_BUSY_CLAIM as a live claim, so no further copy can start.
RUNNER_STUB = r'''
import json
import os
from pathlib import Path
import signal
import sys
import time

root = Path(os.environ["TEST_ROOT"])
arguments = sys.argv[1:]
result = Path(arguments[arguments.index("--result") + 1])
events = root / "runner-events.jsonl"
with events.open("a") as output:
    output.write(json.dumps(arguments) + "\n")
attempt = len(events.read_text().splitlines())
modes = os.environ["TEST_COPY_MODES"].split(",")
mode = modes[min(attempt, len(modes)) - 1]
if mode.endswith("+busy"):
    mode = mode.removesuffix("+busy")
    claims = Path(os.environ["MAC_TEST_STATE_DIR"]) / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / "repo.json").write_text(os.environ["TEST_BUSY_CLAIM"])

def write(mac, reason=""):
    result.write_text("mac=" + mac + "\nreason=" + reason + "\nseconds=4.5\n")

if mode == "hold":
    def stop(signal_number, frame):
        (root / "runner-stopped").write_text("stopped\n")
        raise SystemExit(143)
    signal.signal(signal.SIGTERM, stop)
    (root / "runner-ready").write_text("ready\n")
    while True:
        signal.pause()
if mode == "wait_failed":
    (root / "runner-ready").write_text("ready\n")
    while not (root / "release-runner").exists():
        time.sleep(0.01)
    mode = "failed"
if mode == "failed":
    write("failed")
    raise SystemExit(42)
if mode == "passed":
    write("passed")
    raise SystemExit(0)
if mode == "lost":
    write("lost")
    raise SystemExit(75)
if mode.startswith("declined:"):
    write("declined", mode.removeprefix("declined:"))
    raise SystemExit(75)
if mode == "gave_way":
    raise SystemExit(143)
raise SystemExit(99)
'''


class LineKindTests(unittest.TestCase):
    def test_cargo_and_nextest_lines_are_read_through_ansi_colour(self) -> None:
        cases = {
            "\x1b[1m\x1b[32m   Compiling\x1b[0m sample v0.1.0": "cargo",
            "    Blocking waiting for file lock on build directory": "cargo",
            "    Finished `test` profile [unoptimized] target(s) in 3.2s": "cargo",
            "  Downloaded serde v1.0.0": "cargo",
            "\x1b[32m    Starting\x1b[0m 12 tests across 3 binaries": "tests",
            "    Starting 1 test across 1 binary": "tests",
            "+ cargo nextest run --workspace": "other",
            "Compilingly wrong": "other",
            "    Starting the build": "other",
        }
        for line, kind in cases.items():
            with self.subTest(line=line):
                self.assertEqual(copy_watch.line_kind(line), kind)


class StepProgressTests(unittest.TestCase):
    def test_group_cleanup_keeps_a_matching_zombie_but_rejects_reuse_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / "proc"
            _ = write_stat(proc, 77, start_ticks=123)
            read_stat = process_start.read_stat
            def stat(pid: int, proc_root: Path = process_start.PROC_ROOT) -> process_start.ProcessStat | process_start.ProcessGone:
                _ = proc_root
                return read_stat(pid, proc)
            with mock.patch.object(process_start, "read_stat", side_effect=stat), mock.patch.object(os, "getpgid", return_value=77):
                progress = copy_watch.StepProgress(copy_watch.StepInfo(77, root / "step.log"))
                _ = write_stat(proc, 77, start_ticks=123, state="Z")
                self.assertTrue(progress.leader_alive())
                _ = write_stat(proc, 77, start_ticks=456)
                self.assertFalse(progress.leader_alive())
                with mock.patch.object(process_start, "read_stat", side_effect=process_start.ProcessReadError("denied")):
                    self.assertFalse(progress.leader_alive())

    def test_still_compiling_spans_the_first_cargo_line_to_the_starting_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "step.log"
            _ = log.write_text("+ cargo nextest run\n")
            progress = copy_watch.StepProgress(copy_watch.StepInfo(os.getpid(), log))
            progress.read()
            self.assertFalse(progress.compiling)
            with log.open("a") as output:
                _ = output.write("\x1b[32m   Compil")
            progress.read()
            self.assertFalse(progress.compiling, "a partial line is not read yet")
            with log.open("a") as output:
                _ = output.write("ing\x1b[0m sample\n")
            progress.read()
            self.assertTrue(progress.compiling)
            with log.open("a") as output:
                _ = output.write("\x1b[32m    Starting\x1b[0m 2 tests across 1 binary\n")
            progress.read()
            self.assertFalse(progress.compiling)


class CopyResultTests(unittest.TestCase):
    def test_each_runner_ending_maps_to_one_outcome(self) -> None:
        words = copy_watch.MacWords
        failed, passed = words("failed", "", "1.0"), words("passed", "", "1.0")
        self.assertIsInstance(copy_watch.copy_result(failed, 42), copy_watch.CopyWin)
        self.assertIsInstance(copy_watch.copy_result(passed, 0), copy_watch.CopyWin)
        ended = {
            "stopped by a new test": (copy_watch.copy_result(None, 143), ("gave_way", False)),
            "no result": (copy_watch.copy_result(None, 75), ("lost", False)),
            "pass with a failing status": (copy_watch.copy_result(passed, 1), ("lost", False)),
            "failure with status 0": (copy_watch.copy_result(failed, 0), ("lost", False)),
            "lost": (copy_watch.copy_result(words("lost", "", "1"), 75), ("lost", False)),
            "busy": (copy_watch.copy_result(words("declined", "busy", "0"), 75), ("declined", False)),
            "no tests": (copy_watch.copy_result(words("declined", "no_tests", "0"), 75),
                         ("declined", True)),
            "linux only": (copy_watch.copy_result(words("declined", "linux_only", "0"), 75),
                           ("declined", True)),
        }
        for name, (result, expected) in ended.items():
            with self.subTest(name):
                assert isinstance(result, copy_watch.CopyEnded)
                self.assertEqual((result.outcome, result.final), expected)


class StopTests(unittest.TestCase):
    def test_a_copy_that_ended_before_verifys_term_keeps_its_outcome_unless_it_won(self) -> None:
        cases = {
            "pass that stopped nothing": ("passed", 0, "natedev_won"),
            "busy decline": ("declined", 75, "declined"),
            "failure that stopped nothing": ("failed", 42, "natedev_won"),
        }
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"MAC_TEST_STATE_DIR": directory}):
            root = Path(directory)
            request = copy_watch.WatchRequest(
                info=root / "info", result=root / "result", runner=root / "runner.py",
                settings=root / "mac.conf", repo_root=root, package="sample", call_id="v-call",
                call_started=1.0, filter_run=False, nextest_words=(),
            )
            for name, (mac, status, outcome) in cases.items():
                with self.subTest(name):
                    watch = copy_watch.Watch(request, offload.KnownRepository("repo"), root)
                    result = root / "copy.result"
                    _ = result.write_text(f"mac={mac}\nreason=busy\nseconds=1.0\n")
                    process = subprocess.Popen([sys.executable, "-c", f"raise SystemExit({status})"])
                    _ = process.wait(timeout=HANG_GUARD_S)
                    watch.copy = copy_watch.RunningCopy(process, result, time.monotonic())
                    watch.copy_count = 1
                    report = watch.stop()
                    self.assertEqual((report.outcome, report.copy_count), (outcome, 1))
                    self.assertIsInstance(report.win, copy_watch.NoWin)
                    self.assertIsInstance(watch.copy, copy_watch.NoCopy)


@final
class CandidateTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory = Path()
        self.children: list[subprocess.Popen[bytes]] = []

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.children = []

    @override
    def tearDown(self) -> None:
        for child in self.children:
            child.kill()
            _ = child.wait()

    def live_pid(self) -> int:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.children.append(child)
        return child.pid

    def write(self, call_id: str, pid: int, started: float, proc_start: str | None = None) -> Path:
        path = self.directory / f"{call_id}.json"
        record: copy_watch.CandidateRecord = {
            "pid": pid,
            "proc_start": proc_start if proc_start is not None else cast(process_start.ProcessStart, process_start.process_start(pid)).text,
            "call_started": started,
            "call_id": call_id,
        }
        _ = path.write_text(json.dumps(record))
        return path

    def test_newest_live_candidate_wins_and_dead_records_are_removed(self) -> None:
        _ = self.write("v1", self.live_pid(), 100.0)
        _ = self.write("v2", self.live_pid(), 200.0)
        dead = self.write("v3", self.live_pid(), 300.0, proc_start="0")
        _ = (self.directory / "damaged.json").write_text("{")
        self.assertEqual(copy_watch.newest_candidate(self.directory), "v2")
        self.assertFalse(dead.exists())
        self.assertTrue((self.directory / "damaged.json").exists())

    def test_equal_starts_fall_to_the_larger_call_id(self) -> None:
        _ = self.write("v-a", self.live_pid(), 100.0)
        _ = self.write("v-b", self.live_pid(), 100.0)
        self.assertEqual(copy_watch.newest_candidate(self.directory), "v-b")

    def test_blank_start_keeps_candidate_selection_running_and_preserves_the_record(self) -> None:
        blank = self.write("v-blank", 0, 100.0, proc_start="")
        live = self.write("v-live", self.live_pid(), 200.0)
        self.assertEqual(copy_watch.newest_candidate(self.directory), "v-live")
        self.assertTrue(blank.exists())
        live.unlink()
        self.assertEqual(copy_watch.newest_candidate(self.directory), "v-blank")
        self.assertTrue(blank.exists())

    def test_blank_start_claim_is_never_reclaimed_or_signalled(self) -> None:
        paths = mac_test.StatePaths(self.directory / "state")
        paths.claims.mkdir(parents=True)
        path = paths.claims / "repo.json"
        pid = self.live_pid()
        for start_text in ("", " \t"):
            for remote in (False, True):
                with self.subTest(start=start_text, remote=remote):
                    record: mac_test.RunRecord = {
                        "pid": pid, "proc_start": start_text, "what": "copy",
                        "worktree": str(self.directory), "since": "2026-10-30T00:00:00+00:00",
                        "copy": True,
                    }
                    if remote:
                        record = {**record, "remote_host": "mac"}
                    _ = path.write_text(json.dumps(record))
                    with mock.patch.object(os, "kill") as kill:
                        claims = mac_test.live_claims(paths)
                        self.assertEqual(len(claims), 1)
                        owner = claims[0].record
                        self.assertFalse(mac_test.run_is_kept(owner))
                        self.assertFalse(mac_test.copy_gives_way(owner))
                        mac_test.stop_copy(owner)
                    kill.assert_not_called()
                    self.assertTrue(path.exists())
                    self.assertEqual(owner["proc_start"], start_text)

    def test_no_live_candidate_names_none(self) -> None:
        self.assertIsNone(copy_watch.newest_candidate(self.directory))
        _ = self.write("v1", self.live_pid(), 100.0, proc_start="0")
        self.assertIsNone(copy_watch.newest_candidate(self.directory))


@final
class CopyWatchProcessTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.repository = Path()
        self.state = Path()
        self.info = Path()
        self.result = Path()
        self.runner = Path()
        self.environment: dict[str, str] = {}
        self.step: subprocess.Popen[bytes] | None = None
        self.watcher: subprocess.Popen[str] | None = None

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repository = self.root / "repo"
        (self.repository / ".git").mkdir(parents=True)
        self.state = self.root / "state"
        self.info = self.root / "step-info"
        self.result = self.root / "watch-result"
        self.runner = self.root / "runner.py"
        _ = self.runner.write_text(RUNNER_STUB)
        _ = (self.root / "step.py").write_text(STEP_STUB)
        _ = (self.root / "mac.conf").write_text("offload=on\nlinux_only.repo=\n")
        busy: ClaimRecord = {
            "pid": os.getpid(),
            "proc_start": cast(process_start.ProcessStart, process_start.process_start(os.getpid())).text,
            "what": "nextest other",
            "worktree": str(self.repository),
            "since": datetime.now(timezone.utc).isoformat(),
        }
        self.environment = {
            **os.environ,
            "TEST_ROOT": str(self.root),
            "MAC_TEST_STATE_DIR": str(self.state),
            "MAC_TEST_CONFIG": str(self.root / "mac.conf"),
            "BUILD_TARGETS_POLL_S": "0.05",
            "TEST_BUSY_CLAIM": json.dumps(busy),
            "TEST_COPY_MODES": "failed",
        }
        self.step = None
        self.watcher = None

    @override
    def tearDown(self) -> None:
        for process in (self.watcher, self.step):
            if process is not None and process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=HANG_GUARD_S)
        _ = (self.root / "release-runner").write_text("go\n")

    def start_step(self) -> subprocess.Popen[bytes]:
        log = self.root / "step.log"
        self.step = subprocess.Popen([sys.executable, str(self.root / "step.py"), str(self.root), str(log)],
                                     start_new_session=True)
        _ = self.info.write_text(f"pgid={self.step.pid}\nlog={log}\n")
        return self.step

    def start_watcher(self, modes: str, *, filter_run: bool = False, call_started: str = "1000.5") -> None:
        arguments = [
            sys.executable, str(SCRIPT),
            "--info", str(self.info), "--result", str(self.result), "--runner", str(self.runner),
            "--settings", str(self.root / "mac.conf"), "--repo-root", str(self.repository),
            "--package", "sample", "--call-id", "v-call", "--call-started", call_started,
        ]
        if filter_run:
            arguments.append("--filter-run")
        arguments += ["--", "--no-fail-fast", "--workspace"]
        self.watcher = subprocess.Popen(arguments, env={**self.environment, "TEST_COPY_MODES": modes},
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_until(self, condition: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + HANG_GUARD_S
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    def finish_watcher(self, *, terminate: bool) -> dict[str, str]:
        watcher = self.watcher
        assert watcher is not None
        if terminate:
            watcher.terminate()
        output, _ = watcher.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(watcher.returncode, 0, output)
        lines = self.result.read_text().splitlines()
        return dict(line.split("=", 1) for line in lines)

    def runner_events(self) -> list[list[str]]:
        path = self.root / "runner-events.jsonl"
        if not path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()]

    def candidate(self) -> Path:
        return self.state / "copy_candidates" / "repo" / "v-call.json"

    def compile_and_wait_for_copies(self, count: int) -> None:
        _ = (self.root / "compile").write_text("go\n")
        self.wait_until(lambda: len(self.runner_events()) >= count, f"{count} copies")

    def test_copy_failure_first_stops_the_natedev_group_and_wins(self) -> None:
        step = self.start_step()
        self.start_watcher("failed")
        self.compile_and_wait_for_copies(1)
        values = self.finish_watcher(terminate=False)
        self.assertEqual(step.wait(timeout=HANG_GUARD_S), 143)
        self.assertTrue((self.root / "terminated").exists())
        self.assertTrue(Path(f"{self.info}.stopped").exists())
        self.assertEqual((values["copy"], values["copy_count"], values["mac"], values["runner_status"],
                          values["stopped_natedev"], values["seconds"]), ("won", "1", "failed", "42", "1", "4.5"))
        self.assertGreater(float(values["natedev_s_at_end"]), 0.0)
        self.assertGreaterEqual(float(values["copy_s"]), 0.0)
        arguments = self.runner_events()[0]
        self.assertEqual(arguments[:2], ["run", "--copy"])
        self.assertEqual(arguments[arguments.index("--package") + 1], "sample")
        self.assertEqual(arguments[arguments.index("--call-id") + 1], "v-call")
        self.assertEqual(arguments[arguments.index("--") + 1:], ["--no-fail-fast", "--workspace"])
        self.assertNotIn("--filter-run", arguments)
        self.assertFalse(self.candidate().exists())

    def test_filtered_copy_pass_first_wins(self) -> None:
        step = self.start_step()
        self.start_watcher("passed", filter_run=True)
        self.compile_and_wait_for_copies(1)
        values = self.finish_watcher(terminate=False)
        self.assertEqual(step.wait(timeout=HANG_GUARD_S), 143)
        self.assertEqual((values["copy"], values["mac"], values["runner_status"], values["stopped_natedev"]),
                         ("won", "passed", "0", "1"))
        self.assertIn("--filter-run", self.runner_events()[0])

    def test_full_copy_pass_first_wins(self) -> None:
        step = self.start_step()
        self.start_watcher("passed")
        self.compile_and_wait_for_copies(1)
        values = self.finish_watcher(terminate=False)
        self.assertEqual(step.wait(timeout=HANG_GUARD_S), 143)
        self.assertEqual((values["copy"], values["mac"], values["runner_status"], values["stopped_natedev"]),
                         ("won", "passed", "0", "1"))
        self.assertNotIn("--filter-run", self.runner_events()[0])

    def test_natedev_first_stops_the_running_copy(self) -> None:
        step = self.start_step()
        self.start_watcher("hold")
        self.compile_and_wait_for_copies(1)
        self.wait_until((self.root / "runner-ready").exists, "the copy to run")
        values = self.finish_watcher(terminate=True)
        self.assertTrue((self.root / "runner-stopped").exists())
        self.assertEqual((values["copy"], values["copy_count"]), ("natedev_won", "1"))
        self.assertNotIn("mac", values)
        self.assertIsNone(step.poll())

    def test_result_publication_does_not_recreate_a_removed_hold_folder(self) -> None:
        step = self.start_step()
        hold = self.root / "verify-hold" / "verify-copy"
        hold.mkdir(parents=True)
        self.result = hold / "result"
        self.start_watcher("hold")
        self.compile_and_wait_for_copies(1)
        self.wait_until((self.root / "runner-ready").exists, "the copy to run")
        shutil.rmtree(hold.parent)
        watcher = self.watcher
        assert watcher is not None
        watcher.terminate()
        output, _ = watcher.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(watcher.returncode, 1, output)
        self.assertIn("cannot publish result", output)
        self.assertNotIn("Traceback", output)
        self.assertFalse(hold.parent.exists())
        self.assertTrue((self.root / "runner-stopped").exists())
        self.assertIsNone(step.poll())

    def test_a_copy_that_gives_way_may_become_a_copy_again(self) -> None:
        _ = self.start_step()
        self.start_watcher("gave_way,hold")
        self.compile_and_wait_for_copies(2)
        self.wait_until((self.root / "runner-ready").exists, "the second copy to run")
        values = self.finish_watcher(terminate=True)
        self.assertEqual((values["copy"], values["copy_count"]), ("natedev_won", "2"))

    def test_gave_way_is_the_outcome_when_no_copy_follows(self) -> None:
        _ = self.start_step()
        self.start_watcher("gave_way+busy")
        self.compile_and_wait_for_copies(1)
        self.wait_until(self.candidate().exists, "the call to be a candidate again")
        values = self.finish_watcher(terminate=True)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual((values["copy"], values["copy_count"]), ("gave_way", "1"))

    def test_a_final_decline_ends_copies_and_a_busy_decline_does_not(self) -> None:
        _ = self.start_step()
        self.start_watcher("declined:no_tests")
        self.compile_and_wait_for_copies(1)
        time.sleep(SETTLE_S)
        self.assertFalse(self.candidate().exists())
        values = self.finish_watcher(terminate=True)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual((values["copy"], values["copy_count"]), ("declined", "1"))

        (self.root / "runner-events.jsonl").unlink()
        self.start_watcher("declined:busy,hold")
        self.compile_and_wait_for_copies(2)
        self.wait_until((self.root / "runner-ready").exists, "the second copy to run")
        values = self.finish_watcher(terminate=True)
        self.assertEqual((values["copy"], values["copy_count"]), ("natedev_won", "2"))

    def test_lost_copy_is_recorded_lost(self) -> None:
        _ = self.start_step()
        self.start_watcher("lost+busy")
        self.compile_and_wait_for_copies(1)
        self.wait_until(self.candidate().exists, "the call to be a candidate again")
        values = self.finish_watcher(terminate=True)
        self.assertEqual((values["copy"], values["copy_count"]), ("lost", "1"))

    def test_copy_failure_after_natedev_ended_leaves_natedev_result(self) -> None:
        step = self.start_step()
        self.start_watcher("wait_failed")
        self.compile_and_wait_for_copies(1)
        self.wait_until((self.root / "runner-ready").exists, "the copy to run")
        _ = (self.root / "finish").write_text("go\n")
        self.assertEqual(step.wait(timeout=HANG_GUARD_S), 0)
        _ = (self.root / "release-runner").write_text("go\n")
        values = self.finish_watcher(terminate=False)
        self.assertEqual((values["copy"], values["copy_count"]), ("natedev_won", "1"))
        self.assertNotIn("stopped_natedev", values)
        self.assertFalse(Path(f"{self.info}.stopped").exists())

    def test_tests_started_ends_candidacy_before_the_mac_frees(self) -> None:
        claims = self.state / "claims"
        claims.mkdir(parents=True)
        _ = (claims / "repo.json").write_text(self.environment["TEST_BUSY_CLAIM"])
        _ = self.start_step()
        self.start_watcher("failed")
        _ = (self.root / "compile").write_text("go\n")
        self.wait_until(self.candidate().exists, "the call to be a candidate")
        _ = (self.root / "tests").write_text("go\n")
        self.wait_until(lambda: not self.candidate().exists(), "candidacy to end")
        (claims / "repo.json").unlink()
        time.sleep(SETTLE_S)
        values = self.finish_watcher(terminate=True)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(values, {"copy": "", "copy_s": "", "copy_count": "", "natedev_s_at_end": ""})

    def test_a_newer_live_candidate_takes_the_copy(self) -> None:
        newer = self.state / "copy_candidates" / "repo" / "v-newer.json"
        newer.parent.mkdir(parents=True)
        record: copy_watch.CandidateRecord = {
            "pid": os.getpid(), "proc_start": cast(process_start.ProcessStart, process_start.process_start(os.getpid())).text,
            "call_started": 2000.0, "call_id": "v-newer",
        }
        _ = newer.write_text(json.dumps(record))
        _ = self.start_step()
        self.start_watcher("hold", call_started="1000,5")
        _ = (self.root / "compile").write_text("go\n")
        self.wait_until(self.candidate().exists, "the call to be a candidate")
        time.sleep(SETTLE_S)
        self.assertEqual(self.runner_events(), [])
        newer.unlink()
        self.wait_until(lambda: len(self.runner_events()) == 1, "the copy")
        self.wait_until((self.root / "runner-ready").exists, "the copy to run")
        values = self.finish_watcher(terminate=True)
        self.assertEqual(values["copy"], "natedev_won")

    def test_a_running_copy_blocks_copies_of_other_calls(self) -> None:
        # A copy claim holds the Mac against other copies: free-now --copy says busy.
        claims = self.state / "claims"
        claims.mkdir(parents=True)
        busy = cast(dict[str, object], json.loads(self.environment["TEST_BUSY_CLAIM"]))
        busy["copy"] = True
        _ = (claims / "repo.json").write_text(json.dumps(busy))
        _ = self.start_step()
        self.start_watcher("failed")
        _ = (self.root / "compile").write_text("go\n")
        self.wait_until(self.candidate().exists, "the call to be a candidate")
        time.sleep(SETTLE_S)
        values = self.finish_watcher(terminate=True)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(values["copy"], "")

    def test_signal_ends_the_watcher_at_once(self) -> None:
        # A busy Mac keeps the copy from starting; candidacy shows the handlers are set.
        claims = self.state / "claims"
        claims.mkdir(parents=True)
        _ = (claims / "repo.json").write_text(self.environment["TEST_BUSY_CLAIM"])
        _ = self.start_step()
        self.start_watcher("failed")
        _ = (self.root / "compile").write_text("go\n")
        self.wait_until(self.candidate().exists, "the call to be a candidate")
        started = time.monotonic()
        watcher = self.watcher
        assert watcher is not None
        watcher.send_signal(signal.SIGHUP)
        _ = watcher.communicate(timeout=HANG_GUARD_S)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(self.result.read_text(), "copy=\ncopy_s=\ncopy_count=\nnatedev_s_at_end=\n")


if __name__ == "__main__":
    _ = unittest.main()
