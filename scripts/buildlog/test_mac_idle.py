"""Check idle Mac repairs with private records and fixed clocks."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cli
import index
import mac_idle
import memory
import sample
from test_index import use_test_log

use_test_log()


@final
class MacIdleTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.paths = mac_idle.MacIdlePaths(Path(), Path(), Path(), Path())
        self.call = mac_idle.VerifyTestCall("", 1, "", "", "", "", "", "", "", "", 0, True)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = mac_idle.MacIdlePaths(self.root / "watch", self.root / "mac", self.root / "sessions",
                                        self.root / "buildlog/admission", self.root / "proc", self.root / "registry")
        self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root / "buildlog")}))
        self.paths.proc.mkdir()
        self.paths.mac_state.mkdir()
        self.paths.admission.mkdir(parents=True)
        session = "live-session"
        self.call = mac_idle.VerifyTestCall("v-call", 123, "456", str(self.root / "repo"), "sample", session,
            str(self.paths.session_root / session), str(self.root / "hold-waiter"), str(self.root / "token_waiters"),
            str(self.root / "settings"), 900, True)
        Path(self.call.temp_dir).mkdir(parents=True)
        self.process(123, 1, "456", ("bash", "/repo/scripts/direct/verify.sh", "test", "sample"))
        self.process(122, 1, "455", ("claude",))
        mac_idle.write_record(self.paths.sessions / "122.json", {"sessionId": session, "pid": 122, "procStart": "455"})
        self.process(124, 123, "457", ("bash", "-c", "step"),
                     {"BUILDLOG_CALL_ID": self.call.call_id, "LINT_QUEUE_LEAVE_HOOK": "check Mac"})
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(self.call))
        mac_idle.write_record(self.paths.admission / "124-457.waiting", {"pid": 124, "start_time": "457", "since": 100})
        mac_idle.write_record(self.paths.state / "idle.json", {"since": 1000})

    def process(self, pid: int, parent: int, start: str, argv: tuple[str, ...], env: dict[str, str] | None = None) -> None:
        path = self.paths.proc / str(pid)
        path.mkdir(exist_ok=True)
        fields = ["S", str(parent), *(["0"] * 17), start]
        _ = (path / "stat").write_text(f"{pid} (test) " + " ".join(fields))
        _ = (path / "cmdline").write_bytes(b"\0".join(word.encode() for word in argv))
        _ = (path / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in (env or {}).items()))
        cwd = path / "cwd"
        cwd.unlink(missing_ok=True)
        cwd.symlink_to(self.root)

    def rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()]

    def two_passes(self) -> None:
        mac_idle.watch_once(self.paths, 1180, 100)
        mac_idle.watch_once(self.paths, 1240, 160)

    def test_idle_eligible_wait_gets_one_marker_and_one_row(self) -> None:
        with mock.patch.object(mac_idle, "eligible", return_value=True), \
             mock.patch.object(mac_idle, "notify_director") as send:
            self.two_passes()
        send.assert_not_called()
        self.assertEqual(mac_idle.marker_path(self.paths, self.call.call_id).read_text(), "retry_delayed\n")
        self.assertEqual([(row["cause"], row["action"], row["wait"]) for row in self.rows()],
                         [("retry_delayed", "retry_marker", "memory")])
        self.assertEqual(len(list(self.paths.admission.glob("*.waiting"))), 1)

    def test_worker_session_option_keeps_the_registered_wait_repair(self) -> None:
        self.process(123, 1, "456", ("bash", "/repo/scripts/direct/verify.sh", "--session-dir",
            str(self.root / "worker"), "test", "--no-cache", "sample", "--filter", "smoke"))
        with mock.patch.object(mac_idle, "eligible", return_value=True), \
             mock.patch.object(mac_idle, "notify_director") as send:
            self.two_passes()
        send.assert_not_called()
        self.assertTrue(mac_idle.marker_path(self.paths, self.call.call_id).exists())
        self.assertEqual([(row["package"], row["cause"]) for row in self.rows()], [("sample", "retry_delayed")])

    def test_legacy_worker_options_keep_the_package_and_send_once(self) -> None:
        mac_idle.call_path(self.paths, self.call.call_id).unlink()
        # A session folder named test must not become the subcommand.
        self.process(123, 1, "456", ("bash", "/scripts/verify.sh", "--session-dir", "test",
            "test", "--no-cache", "sample"), {"BUILDLOG_DIR": str(self.root / "buildlog")})
        with mock.patch.object(mac_idle, "eligible", return_value=True) as check, \
             mock.patch.object(mac_idle, "notify_director", return_value=True) as send:
            self.two_passes()
        checked = [cast(mac_idle.VerifyTestCall, call.args[0]) for call in check.call_args_list]
        self.assertEqual([call.package for call in checked], ["sample", "sample"])
        send.assert_called_once()
        self.assertEqual([(row["package"], row["cause"]) for row in self.rows()],
                         [("sample", "call_has_no_marker_hook")])

    def test_legacy_local_option_reaches_eligibility_before_or_after_the_package(self) -> None:
        mac_idle.call_path(self.paths, self.call.call_id).unlink()
        for arguments in (("--local", "test", "--no-cache", "sample"),
                          ("sample", "--no-cache", "--local", "Linux API")):
            with self.subTest(arguments=arguments):
                self.process(123, 1, "456", ("bash", "/scripts/verify.sh", "test", *arguments),
                             {"BUILDLOG_DIR": str(self.root / "buildlog")})
                with mock.patch.object(mac_idle, "eligible", return_value=False) as check, \
                     mock.patch.object(mac_idle, "notify_director") as send:
                    self.two_passes()
                checked = [cast(mac_idle.VerifyTestCall, call.args[0]) for call in check.call_args_list]
                self.assertEqual([(call.package, call.local_reason) for call in checked],
                                 [("sample", "local_flag")] * 2)
                send.assert_not_called()
                self.assertEqual(self.rows(), [])

    def test_live_session_missing_temp_root_is_created(self) -> None:
        Path(self.call.temp_dir).rmdir()
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            self.two_passes()
        self.assertTrue(Path(self.call.temp_dir).is_dir())
        self.assertEqual([row["cause"] for row in self.rows()], ["missing_temp_root", "retry_delayed"])

    def test_any_claim_stops_repairs(self) -> None:
        claims = self.paths.mac_state / "claims"
        claims.mkdir()
        claim = claims / "repo.json"
        _ = claim.write_text("claim must stay unchanged")
        with mock.patch.object(mac_idle, "eligible") as check:
            self.two_passes()
        check.assert_not_called()
        self.assertEqual(claim.read_text(), "claim must stay unchanged")
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
        self.assertEqual(self.rows(), [])

    def test_excluded_package_has_no_action(self) -> None:
        with mock.patch.object(mac_idle, "eligible", return_value=False), \
             mock.patch.object(mac_idle, "notify_director") as send:
            self.two_passes()
        send.assert_not_called()
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
        self.assertEqual(self.rows(), [])

    def test_unknown_cause_sends_once_for_the_call(self) -> None:
        call = replace(self.call, retry_after=0)
        mac_idle.write_record(mac_idle.call_path(self.paths, call.call_id), asdict(call))
        with mock.patch.object(mac_idle, "eligible", return_value=True), \
             mock.patch.object(mac_idle, "notify_director", return_value=True) as send:
            self.two_passes()
        send.assert_called_once_with(call, "idle_with_waiter")
        self.assertEqual([row["action"] for row in self.rows()], ["message_builds_unit"])

    def test_failed_send_stays_one_case_until_it_is_accepted(self) -> None:
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(replace(self.call, retry_after=0)))
        with mock.patch.object(mac_idle, "eligible", return_value=True), \
             mock.patch.object(mac_idle, "notify_director", side_effect=[False, True]) as send:
            self.two_passes()
            mac_idle.watch_once(self.paths, 1300, 200)
        self.assertEqual(send.call_count, 2)
        self.assertEqual([row["action"] for row in self.rows()], ["message_pending"])

    def test_missing_temp_root_is_not_created_for_an_ended_session(self) -> None:
        Path(self.call.temp_dir).rmdir()
        self.process(122, 1, "999", ("claude",))
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            self.two_passes()
        self.assertFalse(Path(self.call.temp_dir).exists())
        self.assertEqual([row["cause"] for row in self.rows()], ["retry_delayed"])

    def test_hold_wait_without_explicit_hook_uses_marker(self) -> None:
        for path in self.paths.admission.iterdir():
            path.unlink()
        self.process(124, 123, "457", (sys.executable, "/scripts/build_targets.py", "wait-local",
            "--call-id", self.call.call_id), {"BUILDLOG_CALL_ID": self.call.call_id})
        mac_idle.write_record(Path(self.call.hold_waiter), {"pid": 124, "proc_start": "457", "call_id": self.call.call_id})
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(replace(self.call, retry_after=0)))
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            self.two_passes()
        self.assertEqual([row["cause"] for row in self.rows()], ["missing_leave_hook"])
        self.assertTrue(mac_idle.marker_path(self.paths, self.call.call_id).exists())

    def test_token_wait_record_is_required(self) -> None:
        for path in self.paths.admission.iterdir():
            path.unlink()
        with mock.patch.object(mac_idle, "eligible") as check:
            self.two_passes()
        check.assert_not_called()
        token = Path(self.call.token_dir)
        token.mkdir()
        _ = (token / "impl.key.stamp.123").write_text("")
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            self.two_passes()
        self.assertEqual([row["wait"] for row in self.rows()], ["cargo_token"])

    def test_pid_reuse_never_repairs_a_call(self) -> None:
        self.process(123, 1, "999", ("bash", "/scripts/verify.sh", "test", "sample"))
        with mock.patch.object(mac_idle, "eligible") as check:
            self.two_passes()
        check.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_legacy_waiting_call_sends_one_message(self) -> None:
        mac_idle.call_path(self.paths, self.call.call_id).unlink()
        self.process(123, 1, "456", ("bash", "/scripts/verify.sh", "test", "sample"),
                     {"BUILDLOG_DIR": str(self.root / "buildlog")})
        with mock.patch.object(mac_idle, "eligible", return_value=True), \
             mock.patch.object(mac_idle, "notify_director", return_value=True) as send:
            self.two_passes()
        send.assert_called_once()
        self.assertEqual([row["cause"] for row in self.rows()], ["call_has_no_marker_hook"])
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())

    def test_routing_wait_requires_a_child_with_the_call_id(self) -> None:
        for path in self.paths.admission.iterdir():
            path.unlink()
        self.process(124, 123, "457", (sys.executable, "/scripts/direct/local_release_wait.py", "15", self.call.call_id))
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            self.two_passes()
        self.assertEqual([row["wait"] for row in self.rows()], ["routing"])

    def test_block_and_unreachable_backoff_stop_repairs(self) -> None:
        for name, text in (("block.json", "{}"), ("unreachable_until", "2000")):
            with self.subTest(name=name):
                path = self.paths.mac_state / name
                _ = path.write_text(text)
                with mock.patch.object(mac_idle, "eligible") as check:
                    self.two_passes()
                check.assert_not_called()
                self.assertEqual(path.read_text(), text)
                path.unlink()

    def test_three_minutes_start_when_idle_is_first_seen(self) -> None:
        (self.paths.state / "idle.json").unlink()
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            mac_idle.watch_once(self.paths, 1000, 100)
            mac_idle.watch_once(self.paths, 1179, 100)
            self.assertEqual(self.rows(), [])
            mac_idle.watch_once(self.paths, 1180, 100)
        self.assertEqual(len(self.rows()), 1)

    def test_claim_folder_change_restarts_the_idle_interval(self) -> None:
        claims = self.paths.mac_state / "claims"
        claims.mkdir()
        os.utime(claims, (1170, 1170))
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            self.two_passes()
            self.assertEqual(self.rows(), [])
            mac_idle.watch_once(self.paths, 1350, 100)
        self.assertEqual(len(self.rows()), 1)

    def test_eligibility_uses_build_targets_command(self) -> None:
        answer = subprocess.CompletedProcess[str]([], 0, "natedev\teligible\t-\t-\nmac\teligible\t-\tsettings\n", "")
        with mock.patch.object(subprocess, "run", return_value=answer) as run:
            self.assertTrue(mac_idle.eligible(self.call))
        command = cast(list[str], run.call_args.args[0])
        self.assertIn("eligible", command)
        self.assertEqual(command[-4:], ["--repo-root", self.call.repo, "--package", self.call.package])

    def test_message_resolves_only_builds_unit_in_build_followups(self) -> None:
        roster = {"entries": [
            {"role": "unit director", "unit": "builds-unit", "production": "other", "address": "session:other"},
            {"role": "worker", "unit": "builds-unit", "production": "build-followups", "address": "worker:impl"},
            {"role": "unit director", "unit": "builds-unit", "production": "build-followups", "address": "session:builds"}]}
        with mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess[str]([], 0, json.dumps(roster), ""),
            subprocess.CompletedProcess[str]([], 0, "SENT: socket", "")]) as run:
            self.assertTrue(mac_idle.notify_director(self.call, "idle_with_waiter"))
        self.assertEqual(run.call_args_list[0].args[0][-1], "--json")
        command = cast(list[str], run.call_args_list[1].args[0])
        self.assertEqual(command[command.index("--to") + 1], "session:builds")
        self.assertNotIn("user", command)

    def test_cases_reach_buildlog_index_and_schema(self) -> None:
        with mock.patch.object(mac_idle, "eligible", return_value=True):
            self.two_passes()
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            self.assertEqual(connection.execute("SELECT call_id, cause, action FROM mac_idle").fetchall(),
                             [(self.call.call_id, "retry_delayed", "retry_marker")])
        self.assertGreaterEqual(index.SCHEMA_VERSION, 20)

    def test_minute_sample_runs_the_watch_on_natedev(self) -> None:
        with mock.patch.object(sample, "sample", return_value=sample.SampleTaken(1000, "natedev")), \
             mock.patch.object(memory, "write_snapshot"), \
             mock.patch.object(mac_idle, "watch_once") as watch:
            self.assertEqual(cli.main(["sample"]), 0)
        watch.assert_called_once()
        self.assertEqual(watch.call_args.args[1], 1000)

    def test_memory_wait_reads_the_marker_when_its_normal_hook_is_empty(self) -> None:
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        marker = self.root / "retry"
        command = '''
set -euo pipefail
source "$1"
buildlog_memory_python() { printf '%s\\n' 'hold|1|1|fallback|0|0|0|0|-||repo|nextest|0|held|worktree'; }
buildlog_memory_line() { :; }
sleep() { : > "$TEST_RETRY_MARKER"; }
BUILDLOG_MAC_IDLE_HOOK='test -f "$TEST_RETRY_MARKER"'
LINT_QUEUE_LEAVE_HOOK=''
buildlog_wait_for_memory cargo nextest run
test "$BUILDLOG_MEM_OUTCOME" = LeftQueue
'''
        result = subprocess.run(["bash", "-c", command, "memory-marker-test",
            str(mac_idle.SCRIPTS / "lint/memory_gate.sh")], env={**os.environ, "BUILDLOG_MEMINFO": str(meminfo),
            "TEST_RETRY_MARKER": str(marker)}, capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    _ = unittest.main()
