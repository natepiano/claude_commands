"""Check idle Mac repairs with private records and fixed clocks."""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Generator
from contextlib import closing, contextmanager, redirect_stderr
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo


from scripts.buildlog import cli
from scripts.buildlog import index
from scripts.buildlog import mac_idle, mac_idle_alert, sample
from scripts.mac_test import offload, mac_test
from scripts.build_queue import admission_protocol as wire, admission_client
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.buildlog.mac_idle_test_support import NotificationGuardedTestCase, NotificationTransportBlocked, PrivateSenderAcceptance, private_sender_acceptance
from scripts.direct import step_record
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture, fake_memory_readings
from scripts.notify.push_texts import catalog, render_entry
from scripts.notify.push_catalog import mac_idle_reservation
from scripts.message import send
from scripts.buildlog.test_index import use_test_log
from scripts.direct.verify_mac_memory_retry_test_support import VerifyMacMemoryRetryFixture
from scripts.lib.process_group import send_process_group

use_test_log()
REAL_NOTIFY_OUTAGE = mac_idle.notify_outage
REAL_PUSH_OUTAGE = mac_idle.push_outage
REAL_SUBPROCESS_RUN = subprocess.run
REAL_SUBPROCESS_POPEN = subprocess.Popen


@final
class MacIdleTests(NotificationGuardedTestCase):
    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.paths = mac_idle.MacIdlePaths(Path(), Path(), Path(), Path())
        self.call = mac_idle.VerifyCall("", 1, "", "", "", "", "", "", "", "", 0, True)

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = mac_idle.MacIdlePaths(self.root / "watch", self.root / "mac", self.root / "sessions",
                                        self.root / "buildlog/admission", self.root / "proc", self.root / "registry")
        self.enterContext(mock.patch.dict(os.environ, {
            **fake_memory_readings(self.root),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "VERIFY_STEPS_DIR": str(self.root / "steps"),
            "VERIFY_PROC_ROOT": str(self.paths.proc),
            "MAC_IDLE_STATE_DIR": str(self.paths.state),
            "MAC_TEST_STATE_DIR": str(self.paths.mac_state),
            "MAC_TEST_CONFIG": str(self.root / "mac_test.conf"),
            "CONF_DIR": str(self.root),
            "SESSION_TEMP_ROOT": str(self.paths.session_root),
            "NOTIFIER_SESSIONS_DIR": str(self.paths.sessions),
            "BUILD_HOLD_DIR": str(self.root / "holds"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_RUN_DIR": str(self.root / "unit"),
        }))
        _ = (self.root / "mac_test.conf").write_text(
            (mac_idle.SCRIPTS.parent / "config/mac_test.conf").read_text())
        self.paths.proc.mkdir()
        self.paths.mac_state.mkdir()
        self.paths.admission.mkdir(parents=True)
        session = "live-session"
        self.call = mac_idle.VerifyCall("v-call", 123, "456", str(self.root / "repo"), "sample", session,
            str(self.paths.session_root / session), str(self.root / "hold-waiter"), str(self.root / "token_waiters"),
            str(self.root / "settings"), 900, True, updated="1970-01-01T00:16:40Z")
        Path(self.call.temp_dir).mkdir(parents=True)
        self.process(123, 1, "456", ("bash", "/repo/scripts/direct/verify.sh", "test", "sample"))
        self.process(122, 1, "455", ("claude",))
        mac_idle.write_record(self.paths.sessions / "122.json", {"sessionId": session, "pid": 122, "procStart": "455"})
        self.process(124, 123, "457", ("bash", "-c", "step"),
                     {"BUILDLOG_CALL_ID": self.call.call_id, "LINT_QUEUE_LEAVE_HOOK": "check Mac"})
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(self.call))
        self.admission = caller_admission(self, self.root, dict(os.environ))
        _ = self.admission.occupy_host()
        with self.admission.loop_turn():
            self.admission.free(0)
            owner = self.admission.fixture_owner(124, "457")
            request = self.admission.request(owner=owner, since=100)
            _ = self.admission.server.handle_request(request)
        mac_idle.write_record(self.paths.state / "idle.json", {"since": 1000})
        self.step()
        _ = self.enterContext(mock.patch.object(mac_idle, "notify_outage", return_value=True))
        _ = self.enterContext(mock.patch.object(mac_test, "run_gh", return_value=mac_test.GhAnswer("[]")))

    def step(self, kind: str = "test", state: step_record.StepState = step_record.StepState.WAITING_HOLD,
             mac_allowed: bool = True, updated: str = "1970-01-01T00:16:40Z") -> None:
        step_record.write(step_record.StepRecord(
            self.call.call_id, self.call.pid, self.call.start, Path(self.call.repo), kind,
            step_record.NamedPackage(self.call.package) if kind == "test" else step_record.WorkspacePackages(),
            step_record.AnyTarget(("mac", "natedev"), ("mac",)) if mac_allowed else step_record.NatedevOnly("Linux only"),
            state, updated))

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
        """Read Mac idle cases from the shared build log."""
        records = [cast(dict[str, object], json.loads(line)) for path in (self.root / "buildlog").glob("*/*.jsonl")
                   for line in path.read_text().splitlines()]
        return [row for row in records if row.get("kind") == "mac_idle"]

    def two_passes(self) -> None:
        mac_idle.watch_once(self.paths, 1180, 100)
        mac_idle.watch_once(self.paths, 1240, 160)

    def test_alert_waits_for_the_call_limit_and_restarts_after_a_state_change(self) -> None:
        self.call = replace(self.call, retry_after=0)
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(self.call))
        state = step_record.state_path(self.call.call_id)
        _ = state.write_text("waiting_hold\n")
        os.utime(state, (1596, 1596))
        limit = mac_idle_alert.read_idle_config().alert_seconds
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as outage, \
             mock.patch.object(mac_idle, "push_outage", return_value=True) as push, \
             mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as repair:
            mac_idle.watch_once(self.paths, 1600, 100)
            outage.assert_not_called()
            push.assert_not_called()
            repair.assert_not_called()
            self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["waiting"], 0)
            self.assertEqual(self.rows(), [])

            _ = state.write_text("on_natedev\n")
            os.utime(state, (1600, 1600))
            mac_idle.watch_once(self.paths, 1600, 100)
            _ = state.write_text("waiting_mac\n")
            os.utime(state, (1600, 1600))
            mac_idle.watch_once(self.paths, 1596 + limit, 200)
            outage.assert_not_called()
            push.assert_not_called()
            repair.assert_not_called()

            mac_idle.watch_once(self.paths, 1600 + limit + 1, 300)
            outage.assert_called_once()
            push.assert_called_once()
            repair.assert_called_once()
            self.assertEqual([row["cause"] for row in self.rows()], ["idle_with_waiter"])

    def test_state_rewrite_keeps_the_first_seen_wait_start(self) -> None:
        state = step_record.state_path(self.call.call_id)
        _ = state.write_text("waiting_mac\n")
        os.utime(state, (1596, 1596))
        limit = mac_idle_alert.read_idle_config().alert_seconds
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as outage, \
             mock.patch.object(mac_idle, "push_outage", return_value=True):
            mac_idle.watch_once(self.paths, 1600, 100)
            outage.assert_not_called()
            now = 1596 + limit + 1
            _ = state.write_text("waiting_mac\n")
            os.utime(state, (now - 10, now - 10))
            mac_idle.watch_once(self.paths, now, 200)
        outage.assert_called_once()
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["waiting"], 1)

    def test_call_that_leaves_the_waiting_set_starts_a_new_wait(self) -> None:
        state = step_record.state_path(self.call.call_id)
        _ = state.write_text("waiting_hold\n")
        os.utime(state, (1596, 1596))
        limit = mac_idle_alert.read_idle_config().alert_seconds
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as outage, \
             mock.patch.object(mac_idle, "push_outage", return_value=True):
            mac_idle.watch_once(self.paths, 1600, 100)
            _ = state.write_text("on_mac\n")
            os.utime(state, (1601, 1601))
            mac_idle.watch_once(self.paths, 1601, 101)
            now = 1600 + limit + 1
            _ = state.write_text("waiting_mac\n")
            os.utime(state, (now - 4, now - 4))
            mac_idle.watch_once(self.paths, now, 200)
            outage.assert_not_called()
            self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["waiting"], 0)
            mac_idle.watch_once(self.paths, now - 4 + limit + 1, 400)
        outage.assert_called_once()

    def test_alert_counts_only_calls_past_the_limit(self) -> None:
        self.waiting_steps(2)
        self.step(updated=datetime.fromtimestamp(1596, timezone.utc).isoformat())
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id),
                              asdict(replace(self.call, local_reason="disk")))
        with mock.patch.object(mac_idle, "notify_outage", side_effect=REAL_NOTIFY_OUTAGE), \
             mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as send, \
             mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
            mac_idle.watch_once(self.paths, 1600, 100)
        saved = mac_idle.read_record(self.paths.state / "alert.json")
        self.assertEqual((saved["waiting"], saved["cause"], saved["detail"]), (1, "free", ""))
        messages = [cast(str, call.args[0]) for call in send.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertIn("1 steps wait for the Mac", messages[0])
        self.assertIn("pid 125", messages[0])
        self.assertNotIn("pid 123", messages[0])
        self.assertNotIn("disk", messages[0])
        push.assert_called_once()
        self.assertEqual([row["call_id"] for row in self.rows()], ["waiting-125"])

    def test_unparseable_wait_time_still_alerts(self) -> None:
        self.step(updated="invalid-time")
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as outage, \
             mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
            mac_idle.watch_once(self.paths, 1600, 100)
        outage.assert_called_once()
        push.assert_called_once()
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["waiting"], 1)

    def test_tree_writing_lint_wait_is_local_and_next_judgment_counts_again(self) -> None:
        verify = mac_idle.SCRIPTS / "direct/verify.sh"
        source = verify.read_text()
        functions: list[str] = []
        for name in ("publish_step_state", "step_state", "step_writes_tree", "finish_step_token", "run", "run_step"):
            start = source.index(f"\n{name}() {{") + 1
            end = source.index("\n}\n", start) + 2
            functions.append(source[start:end])
        script = "\n".join(functions) + r'''
set -uo pipefail
STEP_RECORD_ACTIVE=1; STEP_RUNS_ON=any_target; CMD=lint
TOKEN_USED=0; TOKEN_HELD=0; TOKEN_WAITED=0; LINT_LEFT_QUEUE_STATUS=75
STEP_RECORD_PATH=$TEST_RECORD
STEP_STATE_PATH=${TEST_RECORD%.json}.state
STEP_RECORD_TEXT=$(< "$STEP_RECORD_PATH")
buildlog_step_compiles() { return 0; }
route_compiling_step() {
    STEP_RAN_ON_MAC=0
    step_state waiting_hold
    cp "$TEST_RECORD" "$TEST_REPO/$stage.route"
    if [[ -f "$STEP_STATE_PATH" ]]; then
        cp "$STEP_STATE_PATH" "$TEST_REPO/$stage.route.state"
    fi
}
run_once() {
    step_state waiting_hold
    cp "$TEST_RECORD" "$TEST_REPO/$stage.memory"
    if [[ -f "$STEP_STATE_PATH" ]]; then
        cp "$STEP_STATE_PATH" "$TEST_REPO/$stage.memory.state"
    fi
    return 0
}
for stage in mend fmt fix judgment; do
    case "$stage" in
        mend) run cargo mend --fix ;;
        fmt) run cargo +nightly fmt ;;
        fix) run cargo clippy --fix ;;
        judgment) run cargo clippy -- -D warnings ;;
    esac || exit $?
done
'''
        Path(self.call.repo).mkdir()
        result = subprocess.run(["bash", "-c", script], env={**os.environ,
            "PY": str(mac_idle.SCRIPTS / "lib/py"), "STEP_RECORD_SCRIPT": str(verify.with_name("step_record.py")),
            "BUILDLOG_CALL_ID": self.call.call_id, "TEST_REPO": self.call.repo,
            "TEST_RECORD": str(step_record.record_path(self.call.call_id))},
            capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        live = mac_idle.processes(self.paths.proc)
        for stage in ("mend", "fmt", "fix", "judgment"):
            for wait in ("route", "memory"):
                with self.subTest(stage=stage, wait=wait):
                    snapshot = Path(self.call.repo) / f"{stage}.{wait}"
                    _ = step_record.record_path(self.call.call_id).write_bytes(snapshot.read_bytes())
                    state_snapshot = Path(str(snapshot) + ".state")
                    state = step_record.state_path(self.call.call_id)
                    if state_snapshot.exists():
                        _ = state.write_bytes(state_snapshot.read_bytes())
                        os.utime(state, (1000, 1000))
                    else:
                        state.unlink(missing_ok=True)
                    record = step_record.read(self.call.call_id)
                    self.assertIsInstance(record, step_record.StepRecord)
                    assert isinstance(record, step_record.StepRecord)
                    if stage == "judgment":
                        self.assertIsInstance(record.runs_on, step_record.AnyTarget)
                        self.assertEqual(len(mac_idle.waiting_calls(self.paths, live)), 1)
                        mac_idle.call_path(self.paths, self.call.call_id).with_suffix(".cases").unlink(missing_ok=True)
                        with mock.patch.object(mac_idle, "record_case") as report:
                            mac_idle.watch_once(self.paths, 1180, 100)
                        self.assertTrue(report.called)
                    else:
                        self.assertIsInstance(record.runs_on, step_record.NatedevOnly)
                        self.assertEqual(mac_idle.waiting_calls(self.paths, live), ())
                        with mock.patch.object(mac_idle, "notify_showrunner") as send:
                            mac_idle.watch_once(self.paths, 1180, 100)
                        send.assert_not_called()
                        self.assertEqual(self.rows(), [])
    def test_blocked_mac_reports_three_waiting_steps_after_three_minutes(self) -> None:
        self.block()
        self.waiting_steps(3)
        with mock.patch.object(mac_idle, "notify_outage", side_effect=REAL_NOTIFY_OUTAGE), mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess[str]([], 0, "uds:/tmp/builds-showrunner.sock\n", ""),
            subprocess.CompletedProcess[str]([], 0, "SENT: socket", "")]) as run:
            mac_idle.watch_once(self.paths, 1180, 100)
        saved = mac_idle.read_record(self.paths.state / "alert.json")
        self.assertEqual((saved.get("state"), saved.get("cause"), saved.get("holder"), saved.get("waiting")),
                         ("idle", "blocked", "research", 3))
        command = cast(list[str], run.call_args_list[1].args[0])
        self.assertEqual(command[command.index("--to") + 1], "uds:/tmp/builds-showrunner.sock")
        self.assertIn("wait memory", command[command.index("--text") + 1])
        self.assertNotIn("user", command)

    def waiting_steps(self, total: int) -> None:
        for pid in range(125, 125 + total - 1):
            self.process(pid, 1, str(pid), ("bash", "step"))
            step_record.write(step_record.StepRecord(
                f"waiting-{pid}", pid, str(pid), Path(self.call.repo), "test", step_record.NamedPackage("sample"),
                step_record.AnyTarget(("mac", "natedev"), ("mac",)), step_record.StepState.WAITING_HOLD, "1970-01-01T00:16:40Z"))

    def claim(self, *, working: bool = False, remote: bool = True, work_idle_since: float = 1000,
              name: str = "research", since: float = 900) -> Path:
        row: dict[str, object] = {"claim_id": f"claim-{name}", "episode": f"episode-{name}",
            "attempt": "fixture-attempt", "submitting_session": "research",
            "pid": os.getpid(), "proc_start": "ended-owner", "what": "P15 F11 L0 run",
            "worktree": str(self.root / name), "since": datetime.fromtimestamp(since, timezone.utc).isoformat(),
            "holder_session": "research"}
        if remote:
            row["remote_host"] = "mac"
        if not working:
            row["idle_since"] = "2026-10-31T07:50:00-04:00"
            row["work_idle_since"] = datetime.fromtimestamp(work_idle_since, timezone.utc).isoformat()
        path = self.paths.mac_state / "claims" / (name + ".json")
        mac_idle.write_record(path, row)
        return path

    def test_block_wins_over_an_idle_claim_but_work_ends_the_outage(self) -> None:
        self.waiting_steps(3)
        _ = self.claim()
        self.block(detail="research run")
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as notify, mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
            mac_idle.watch_once(self.paths, 1180, 100)
            saved = mac_idle.read_record(self.paths.state / "alert.json")
            self.assertEqual((saved["state"], saved["cause"]), ("idle", "blocked"))
            notify.assert_called_once()
            push.assert_not_called()
            _ = self.claim(working=True)
            mac_idle.watch_once(self.paths, 1240, 100)
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["state"], "busy")
        self.assertFalse((self.paths.state / "idle.json").exists())
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())

    def block(self, holder: str = "research", detail: str = "P15 F11 L0 run", *, terminal: bool = False) -> None:
        since = datetime.fromtimestamp(1000, timezone.utc)
        delivery = (mac_test.HolderQueue(holder) if terminal else
                    mac_test.RecordedSession(holder, holder))
        block = mac_test.ActiveMacBlock(holder, detail, since,
            since + timedelta(hours=4), delivery, mac_test.NO_SHOWRUNNER,
            "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED)
        mac_idle.write_record(self.paths.mac_state / "block.json", mac_test.block_record(block))

    def test_terminal_block_from_real_encoder_alerts_instead_of_becoming_unreadable(self) -> None:
        self.block(terminal=True)
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as notify:
            mac_idle.watch_once(self.paths, 1180, 100)
        saved = mac_idle.read_record(self.paths.state / "alert.json")
        self.assertEqual((saved["state"], saved["cause"], saved["holder"]), ("idle", "blocked", "research"))
        notify.assert_called_once()

    def test_every_idle_pass_blocks_real_phone_and_showrunner_transports(self) -> None:
        self.block()
        for stage in ("notify_outage", "push_outage"):
            with self.subTest(stage=stage):
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                other = "push_outage" if stage == "notify_outage" else "notify_outage"
                # This inner tripwire makes the regression safe even if the setUp guard is removed.
                transport = REAL_NOTIFY_OUTAGE if stage == "notify_outage" else REAL_PUSH_OUTAGE
                with mock.patch.object(mac_idle, other, return_value=True), \
                     mock.patch.object(mac_idle, stage, side_effect=transport), \
                     mock.patch.object(subprocess, "Popen", side_effect=AssertionError("unguarded spawn")):
                    with self.assertRaises(NotificationTransportBlocked):
                        mac_idle.watch_once(self.paths, 1600, 100)

    def sender_command(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--key" not in command:
            return subprocess.CompletedProcess(command, 0, "uds:/fixture/builds.sock", "")
        options = send.parse(command[2:])._replace(shown_sender=send.ShownSender("Mac idle watch"))
        result = send.send(options)
        return subprocess.CompletedProcess(command, send.EXIT[result.outcome], result.outcome.upper(), result.detail)

    @contextmanager
    def outage_sender(self) -> Generator[tuple[PrivateSenderAcceptance, mock.MagicMock, mock.MagicMock]]:
        with private_sender_acceptance(self.root / "sender") as fixture, \
             mock.patch.object(mac_idle, "notify_outage", side_effect=REAL_NOTIFY_OUTAGE), \
             mock.patch.object(mac_idle, "push_outage", return_value=True) as push, \
             mock.patch.object(subprocess, "run", side_effect=self.sender_command) as run:
            yield fixture, push, run

    def sent_keys(self, run: mock.MagicMock) -> list[str]:
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list if "--key" in call.args[0]]
        return [command[command.index("--key") + 1] for command in commands]

    def check_interrupted_delivery(self, *, after_sender_record: bool) -> None:
        class Stopped(BaseException):
            pass

        def stop(*_args: object, **_kwargs: object) -> None:
            raise Stopped()

        self.block()
        settings = mac_idle_alert.read_idle_config()
        for stage, mark in (("notify_outage", "messaged"), ("push_outage", "pushed")):
            with self.subTest(stage=stage, after_sender_record=after_sender_record):
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                other = "push_outage" if stage == "notify_outage" else "notify_outage"
                with private_sender_acceptance(self.root / stage) as fixture, \
                     mock.patch.object(mac_idle, other, return_value=True), \
                     mock.patch.object(mac_idle, stage, side_effect=REAL_NOTIFY_OUTAGE if stage == "notify_outage" else REAL_PUSH_OUTAGE), \
                     mock.patch.object(subprocess, "run", side_effect=self.sender_command):
                    target = "log" if after_sender_record else "run"
                    with mock.patch.object(send, target, side_effect=stop), self.assertRaises(Stopped):
                        mac_idle.watch_once(self.paths, 1600, 100)
                    fixture.end_owner()
                    self.assertEqual(fixture.count(), 1 if after_sender_record else 0)
                    self.assertIs(mac_idle.read_record(self.paths.state / "alert.json")[mark], False)
                    current = 1600 + settings.repeat_minutes * 120
                    mac_idle.watch_once(self.paths, current, 160)
                    mac_idle.watch_once(self.paths, current + 60, 220)
                    self.assertEqual(fixture.count(), 1)
                self.assertIs(mac_idle.read_record(self.paths.state / "alert.json")[mark], True)

    def test_notification_marks_survive_stop_after_delivery_before_return(self) -> None:
        self.check_interrupted_delivery(after_sender_record=True)

    def test_stop_before_transport_retries_and_delivers_once(self) -> None:
        self.check_interrupted_delivery(after_sender_record=False)

    def test_contended_sender_never_marks_the_outage_done(self) -> None:
        self.block()
        for stage, mark, channel in (("notify_outage", "messaged", "message"), ("push_outage", "pushed", "push")):
            with self.subTest(stage=stage):
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                other = "push_outage" if stage == "notify_outage" else "notify_outage"
                def while_held() -> None:
                    mac_idle.watch_once(self.paths, 1600, 100)
                    self.assertIs(mac_idle.read_record(self.paths.state / "alert.json")[mark], False)

                with private_sender_acceptance(self.root / stage, before_accept=while_held) as fixture, \
                     mock.patch.object(mac_idle, other, return_value=True), \
                     mock.patch.object(mac_idle, stage, side_effect=REAL_NOTIFY_OUTAGE if stage == "notify_outage" else REAL_PUSH_OUTAGE), \
                     mock.patch.object(subprocess, "run", side_effect=self.sender_command), redirect_stderr(io.StringIO()):
                    key = "mac-idle-showrunner-1000-1000" if channel == "message" else "mac-idle-1000"
                    options = fixture.options(channel)._replace(key=key)
                    if channel == "message":
                        options = options._replace(recipient_identity=send.LogicalRecipientIdentity("showrunner:builds"))
                    self.assertEqual(send.send(options).outcome, "sent")
                    mac_idle.watch_once(self.paths, 1660, 160)
                    self.assertEqual(fixture.count(), 1)
                    self.assertIs(mac_idle.read_record(self.paths.state / "alert.json")[mark], True)

    def test_restoring_subprocess_transports_cannot_bypass_notification_guard(self) -> None:
        # The script paths are inert arguments to -c, so removing the guard cannot send anything.
        for script in ("message/send.py", "production/showrunners.py"):
            with self.subTest(script=script), \
                 mock.patch.object(subprocess, "run", wraps=REAL_SUBPROCESS_RUN), \
                 mock.patch.object(subprocess, "Popen", wraps=REAL_SUBPROCESS_POPEN):
                with self.assertRaises(NotificationTransportBlocked):
                    _ = subprocess.run([sys.executable, "-c", "pass", str(mac_idle.SCRIPTS / script)],
                                       check=False, capture_output=True, text=True, timeout=30)

    def test_replacement_block_refreshes_resolved_holder_in_same_outage(self) -> None:
        self.block("research")
        mac_idle.watch_once(self.paths, 1180, 100)
        self.block("release")
        mac_idle.watch_once(self.paths, 1240, 160)
        saved = mac_idle.read_record(self.paths.state / "alert.json")
        self.assertEqual((saved["since"], saved["holder"]), (1000, "release"))

    def test_huge_idle_since_is_reset_without_stopping_the_watch(self) -> None:
        mac_idle.write_record(self.paths.state / "idle.json", {"since": 10 ** 400})
        mac_idle.watch_once(self.paths, 1180, 100)
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["since"], 1180)

    def test_busy_mac_with_no_steps_never_scans_all_processes(self) -> None:
        step_record.record_path(self.call.call_id).unlink(missing_ok=True)
        _ = self.claim(working=True)
        with mock.patch.object(mac_idle, "processes", side_effect=AssertionError("unneeded proc scan")):
            mac_idle.watch_once(self.paths, 1180, 100)
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["state"], "busy")

    def test_busy_or_young_outage_with_steps_never_scans_all_processes(self) -> None:
        self.step(updated="1970-01-01T00:15:00Z")
        with mock.patch.object(mac_idle, "processes", side_effect=AssertionError("unneeded proc scan")):
            mac_idle.watch_once(self.paths, 1100, 100)
            saved = mac_idle.read_record(self.paths.state / "alert.json")
            self.assertEqual((saved["state"], saved["waiting"]), ("idle", 1))
            _ = self.claim(working=True)
            mac_idle.watch_once(self.paths, 1600, 100)
        saved = mac_idle.read_record(self.paths.state / "alert.json")
        self.assertEqual((saved["state"], saved["waiting"]), ("busy", 1))

    def test_ci_mac_work_controls_idle_alerts_only_while_steps_wait(self) -> None:
        for block_kind, job_status, steps_wait, state, cause, lookups in (
            ("ci_mac_job", "in_progress", True, "busy", "", 0),
            ("ci_job", "queued", True, "idle", "blocked", 0),
            ("", "in_progress", True, "busy", "", 3),
            ("", "completed", True, "idle", "free", 3),
            ("", "unknown", True, "idle", "free", 1),
            ("", "in_progress", False, "idle", "free", 0),
            ("active", "in_progress", True, "idle", "blocked", 0),
            ("claim", "in_progress", True, "idle", "claim_idle", 0),
            ("", "queued before running", True, "busy", "", 4),
        ):
            with self.subTest(block=block_kind, job=job_status, steps_wait=steps_wait):
                (self.paths.mac_state / "block.json").unlink(missing_ok=True)
                (self.paths.mac_state / "claims/research.json").unlink(missing_ok=True)
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                mac_idle.marker_path(self.paths, self.call.call_id).unlink(missing_ok=True)
                mac_idle.write_record(self.paths.state / "idle.json", {"since": 1000})
                self.step()
                if not steps_wait:
                    step_record.record_path(self.call.call_id).unlink()
                if block_kind in ("ci_job", "ci_mac_job"):
                    since = datetime.fromtimestamp(1000, timezone.utc)
                    waiting = (mac_test.WAITING_ON_CI_MAC_JOB if block_kind == "ci_mac_job"
                               else mac_test.WAITING_ON_CI_JOB)
                    block = mac_test.PendingMacBlock("research", "render", since, since + timedelta(hours=4),
                        mac_test.HolderQueue("research"), mac_test.NO_SHOWRUNNER, "off_before", waiting)
                    mac_test.write_block(self.paths.mac_state / "block.json", block)
                elif block_kind == "active":
                    self.block()
                elif block_kind == "claim":
                    _ = self.claim()

                def gh(config: mac_test.MacTestConfig, *arguments: str) -> mac_test.GhResult:
                    if job_status == "unknown":
                        return mac_test.GhFailure("API unavailable")
                    if arguments[:2] == ("run", "list"):
                        status = arguments[arguments.index("--status") + 1]
                        runs = ([{"databaseId": 40 if status == "queued" else 41,
                                  "createdAt": datetime.now(timezone.utc).isoformat()}]
                                if status == "in_progress" or job_status == "queued before running" else [])
                        return mac_test.GhAnswer(json.dumps(runs))
                    status = ("queued" if arguments[2] == "40" else "in_progress"
                              if job_status == "queued before running" else job_status)
                    return mac_test.GhAnswer(json.dumps({"jobs": [{"name": config.ci_job, "status": status}]}))

                with mock.patch.object(mac_test, "run_gh", side_effect=gh) as query, \
                     mock.patch.object(mac_idle, "notify_outage", return_value=True) as notify, \
                     mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
                    mac_idle.watch_once(self.paths, 1600, 100)
                saved = mac_idle.read_record(self.paths.state / "alert.json")
                self.assertEqual((saved["state"], saved["cause"]), (state, cause))
                self.assertEqual(query.call_count, lookups)
                if state == "busy":
                    notify.assert_not_called()
                    push.assert_not_called()
                    self.assertIsNone(mac_idle_alert.alert_line(self.paths.state / "alert.json", 1600, ZoneInfo("UTC")))
                    self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
                elif steps_wait:
                    notify.assert_called_once()
                    push.assert_called_once()
                else:
                    notify.assert_not_called()
                    push.assert_not_called()

    def test_missing_or_bad_idle_configuration_produces_a_watch_diagnostic(self) -> None:
        self.block()
        mac_idle.watch_once(self.paths, 1180, 100)
        read_text = Path.read_text
        config_paths = {self.root / "mac_test.conf", mac_idle.SCRIPTS.parent / "config/mac_test.conf"}
        for bad in ("", "alert_seconds=bad\n"):
            def damaged_config(path: Path, *, encoding: str = "utf-8") -> str:
                return bad if path in config_paths else read_text(path, encoding=encoding)

            with self.subTest(config=bad), mock.patch.object(Path, "read_text", autospec=True,
                                   side_effect=damaged_config), \
                 mock.patch.object(conf, "_committed_files", return_value=()):
                line = mac_idle_alert.alert_line(self.paths.state / "alert.json", 1180, ZoneInfo("UTC"))
                self.assertIn("MAC IDLE WATCH: config unreadable:", line or "")
                with self.assertRaises(mac_idle_alert.MacIdleConfigError):
                    _ = mac_test.read_config()

    def test_repair_text_uses_configured_idle_threshold(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "alert_seconds=42\n")
        message = mac_idle.repair_message(self.call, "idle_with_waiter")
        self.assertIn("at least 42 seconds", message)
        self.assertNotIn("3 minutes", message)

    def test_config_changes_apply_to_live_outage_views_push_and_clear_check(self) -> None:
        _ = self.claim()
        config = self.root / "mac_test.conf"
        _ = write_config_fixture(config, "alert_seconds=240\npush_seconds=300\n"
                              + "stale_seconds=30\nrepeat_minutes=37\nsend_timeout_s=9\n")
        path = self.paths.state / "alert.json"
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as notify, \
             mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "SENT", "")) as run:
            mac_idle.watch_once(self.paths, 1180, 100)
            notify.assert_not_called()
            self.assertIsNone(mac_idle_alert.alert_line(path, 1180, ZoneInfo("UTC")))
            mac_idle.watch_once(self.paths, 1240, 160)
            notify.assert_called_once()
            self.assertIn("Claim of research", mac_idle_alert.alert_line(path, 1240, ZoneInfo("UTC")) or "")
            run.assert_not_called()
            # A change to the selected file takes effect without restarting the watch.
            _ = config.write_text(config.read_text().replace("push_seconds=300", "push_seconds=241"))
            mac_idle.watch_once(self.paths, 1241, 161)
            run.assert_called_once()
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(command[command.index("--repeat-minutes") + 1], "37")
            self.assertEqual(run.call_args.kwargs["timeout"], 9)
        self.assertIn("no reading since", mac_idle_alert.alert_line(path, 1272, ZoneInfo("UTC")) or "")
        with mock.patch.object(time, "time", return_value=1272):
            self.assertEqual(mac_idle.main(["cleared", "--since", "999"]), 1)

    def test_ten_minutes_pushes_once_and_next_outage_uses_new_key(self) -> None:
        self.block()
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "SENT", "")) as run:
            mac_idle.watch_once(self.paths, 1600, 100)
            mac_idle.watch_once(self.paths, 1660, 100)
            run.assert_called_once()
            command = cast(list[str], run.call_args.args[0])
            for flag, value in (("--text-id", "mac_idle.blocked"), ("--need", "blocked"), ("--key", "mac-idle-1000")):
                self.assertEqual(command[command.index(flag) + 1], value)
            self.assertEqual(command[command.index("--clears-when") + 1],
                f"{mac_idle.SCRIPTS}/lib/py {mac_idle.SCRIPTS}/buildlog/mac_idle.py cleared --since 1000")
            _ = self.claim(working=True)
            mac_idle.watch_once(self.paths, 1700, 100)
            (self.paths.mac_state / "claims/research.json").unlink()
            mac_idle.watch_once(self.paths, 1760, 100)
            mac_idle.watch_once(self.paths, 2360, 100)
            self.assertEqual(run.call_count, 2)
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(command[command.index("--key") + 1], "mac-idle-1760")
        saved = mac_idle.read_record(self.paths.state / "alert.json")
        self.assertEqual((saved["messaged"], saved["pushed"]), (True, True))

    def test_parked_app_claim_alert_names_holder_and_never_repairs(self) -> None:
        self.waiting_steps(7)
        _ = self.claim()
        with mock.patch.object(mac_idle, "unit_labels_for", return_value={}) as labels, \
             mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "SENT", "")) as run:
            mac_idle.watch_once(self.paths, 1180, 100)
            mac_idle.watch_once(self.paths, 1600, 100)
            labels.assert_called_once_with(["research"], os.environ)
            command = cast(list[str], run.call_args.args[0])
            self.assertEqual(command[command.index("--text-id") + 1], "mac_idle.stuck")
            self.assertEqual(command[command.index("--need") + 1], "decision")
            self.assertEqual(command[command.index("--repeat-minutes") + 1], "1440")
        line = mac_idle_alert.alert_line(self.paths.state / "alert.json", 1600, ZoneInfo("UTC"))
        self.assertIn("Claim of research (P15 F11 L0 run)", line or "")
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
        self.assertEqual(self.rows(), [])

    def test_free_idle_push_requests_a_decision(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess[str]([], 0, "SENT", "")) as run:
            mac_idle.watch_once(self.paths, 1600, 100)
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[command.index("--text-id") + 1], "mac_idle.stuck")
        self.assertIn("--repeat-minutes", command)
        self.assertNotIn("--clears-when", command)

    def test_push_failures_retry_same_key_until_accepted(self) -> None:
        self.block(detail="run")
        for code in (4, 3, 2):
            with self.subTest(code=code), mock.patch.object(subprocess, "run", side_effect=[
                subprocess.CompletedProcess[str]([], code, "not delivered", ""),
                subprocess.CompletedProcess[str]([], 0, "SENT", "")]) as run, redirect_stderr(io.StringIO()) as errors:
                mac_idle.write_record(self.paths.state / "idle.json", {"since": 1000 + code})
                mac_idle.watch_once(self.paths, 1700, 100)
                self.assertIs(mac_idle.read_record(self.paths.state / "alert.json")["pushed"], False)
                mac_idle.watch_once(self.paths, 1760, 100)
                mac_idle.watch_once(self.paths, 1820, 100)
                self.assertEqual(run.call_count, 2)
                commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
                self.assertEqual([command[command.index("--key") + 1] for command in commands],
                                 [f"mac-idle-{1000 + code}"] * 2)
                self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_showrunner_resolution_and_send_failures_retry_without_duplicate_success(self) -> None:
        self.block()
        answers = [subprocess.CompletedProcess[str]([], 0, "", ""),
                   subprocess.CompletedProcess[str]([], 0, "uds:/tmp/builds.sock", ""),
                   subprocess.CompletedProcess[str]([], 3, "FAILED", ""),
                   subprocess.CompletedProcess[str]([], 0, "uds:/tmp/builds.sock", ""),
                   subprocess.CompletedProcess[str]([], 1, "QUEUED", "")]
        with mock.patch.object(mac_idle, "notify_outage", side_effect=REAL_NOTIFY_OUTAGE), \
             mock.patch.object(subprocess, "run", side_effect=answers) as run, redirect_stderr(io.StringIO()) as errors:
            for now in (1180, 1240, 1300, 1360):
                mac_idle.watch_once(self.paths, now, 100)
        self.assertEqual(run.call_count, 5)
        self.assertEqual(len(errors.getvalue().splitlines()), 2)
        self.assertEqual(self.sent_keys(run), ["mac-idle-showrunner-1000-1000"] * 2)

    def test_excluded_and_natedev_only_steps_do_not_alert(self) -> None:
        for reason in ("no_tests", "off", "repo", "linux_only", "local_flag"):
            with self.subTest(reason=reason), mock.patch.object(mac_idle, "notify_outage") as notify, \
                 mock.patch.object(mac_idle, "push_outage") as push:
                mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(replace(self.call, local_reason=reason)))
                mac_idle.watch_once(self.paths, 1600, 100)
                self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["waiting"], 0)
                self.assertIsNone(mac_idle_alert.alert_line(self.paths.state / "alert.json", 1600, ZoneInfo("UTC")))
                notify.assert_not_called()
                push.assert_not_called()

    def test_disk_exclusion_alerts_without_retrying_the_excluded_call(self) -> None:
        for state in (step_record.StepState.WAITING_HOLD, step_record.StepState.WAITING_MAC,
                      step_record.StepState.ON_NATEDEV):
            with self.subTest(state=state):
                step_record.write(step_record.StepRecord(
                    self.call.call_id, self.call.pid, self.call.start, Path(self.call.repo), "test",
                    step_record.NamedPackage(self.call.package), step_record.NatedevOnly("disk"), state, "1970-01-01T00:16:40Z"))
                mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id),
                                      asdict(replace(self.call, local_reason="disk")))
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                with mock.patch.object(mac_idle, "notify_outage", side_effect=REAL_NOTIFY_OUTAGE), \
                     mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as notify, \
                     mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
                    mac_idle.watch_once(self.paths, 1600, 100)
                saved = mac_idle.read_record(self.paths.state / "alert.json")
                self.assertEqual((saved["waiting"], saved["cause"]), (1, "disk"))
                self.assertIn("disk", mac_idle_alert.alert_line(
                    self.paths.state / "alert.json", 1600, ZoneInfo("UTC")) or "")
                notify.assert_called_once()
                message = cast(str, notify.call_args.args[0])
                self.assertIn("disk", message)
                push.assert_called_once()
                self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
                self.assertEqual(self.rows(), [])

    def test_eligibility_disk_exclusion_survives_local_compilation_and_alerts(self) -> None:
        environment = dict(os.environ)
        fixture = VerifyMacMemoryRetryFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.environment["VERIFY_PROC_ROOT"] = "/proc"
        _ = fixture.hold_memory()
        fixture.write_executable(fixture.bin_directory / "ssh",
                                 "#!/bin/sh\nprintf 'free_gib=0\\nhost=test-mac\\nrc=0\\n'\n")
        process = fixture.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(fixture.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": "disk"})
        self.addCleanup(fixture.stop_verify, process)
        fixture.wait_for_memory_wait(process)
        records = list((fixture.root / "verify-steps").glob("*.json"))
        self.assertEqual(len(records), 1)
        with mock.patch.dict(os.environ, {
            "VERIFY_STEPS_DIR": str(records[0].parent), "VERIFY_PROC_ROOT": "/proc"}):
            record = step_record.read(records[0].stem)
        if not isinstance(record, step_record.StepRecord):
            self.fail(f"Expected a live step record, got {record!r}")
        self.assertEqual(record.mac_disk_exclusions, ("mac",))
        self.assertIsInstance(record.runs_on, step_record.AnyTarget)
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        step_record.record_path(self.call.call_id).unlink()
        destination = step_record.record_path(record.call_id)
        _ = destination.write_bytes(records[0].read_bytes())
        self.process(record.pid, 1, record.proc_start, ("bash", "verify.sh", "test", "sample"))
        observed_at = datetime.fromisoformat(record.updated).timestamp() + 600
        for state in ("waiting_hold", "waiting_mac", "on_natedev", "writes_tree on_natedev"):
            with self.subTest(state=state):
                _ = step_record.state_path(record.call_id).write_text(state + "\n")
                decoded = step_record.read(record.call_id)
                if not isinstance(decoded, step_record.StepRecord):
                    self.fail(f"Expected a live step record, got {decoded!r}")
                self.assertEqual(decoded.mac_disk_exclusions, ("mac",))
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                with mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as notify, \
                     mock.patch.object(mac_idle, "notify_outage", side_effect=REAL_NOTIFY_OUTAGE), \
                     mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
                    mac_idle.watch_once(self.paths, observed_at, 100)
                saved = mac_idle.read_record(self.paths.state / "alert.json")
                if state == "writes_tree on_natedev":
                    self.assertEqual((saved["waiting"], saved["cause"]), (0, "free"))
                    self.assertIsNone(mac_idle_alert.alert_line(
                        self.paths.state / "alert.json", observed_at, ZoneInfo("UTC")))
                    notify.assert_not_called()
                    push.assert_not_called()
                else:
                    self.assertEqual((saved["waiting"], saved["cause"]), (1, "disk"))
                    self.assertIn("disk", mac_idle_alert.alert_line(
                        self.paths.state / "alert.json", observed_at, ZoneInfo("UTC")) or "")
                    notify.assert_called_once()
                    push.assert_called_once()

    def test_disk_evidence_does_not_make_natedev_only_steps_wait_for_the_mac(self) -> None:
        for kind, reason in (("lint", "step writes the tree"), ("test", "local files"),
                             ("build", "makes natedev artifacts"), ("final", "full gate runs on natedev")):
            with self.subTest(kind=kind, reason=reason):
                step_record.write(step_record.StepRecord(
                    self.call.call_id, self.call.pid, self.call.start, Path(self.call.repo), kind,
                    step_record.NamedPackage(self.call.package), step_record.NatedevOnly(reason),
                    step_record.StepState.ON_NATEDEV, "1970-01-01T00:16:40Z", mac_disk_exclusions=("mac",)))
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                with mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as notify, \
                     mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
                    mac_idle.watch_once(self.paths, 1600, 100)
                self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["waiting"], 0)
                notify.assert_not_called()
                push.assert_not_called()

    def test_disk_alert_clears_when_the_floor_sweep_allows_the_step(self) -> None:
        step_record.write(step_record.StepRecord(
            self.call.call_id, self.call.pid, self.call.start, Path(self.call.repo), "test",
            step_record.NamedPackage(self.call.package), step_record.NatedevOnly("disk"), step_record.StepState.ON_NATEDEV,
            "1970-01-01T00:16:40Z"))
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id),
                              asdict(replace(self.call, local_reason="disk")))
        with mock.patch.object(mac_idle, "push_outage", return_value=True) as push:
            mac_idle.watch_once(self.paths, 1600, 100)
        push.assert_called_once()
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["cause"], "disk")
        config = offload.read_config(self.root / "mac_test.conf")
        with (mock.patch.object(offload.ProcessController, "capture", return_value=offload.CommandOutput(0, "", "")),
              mock.patch.object(offload, "probe_mac", return_value=offload.ProbeSnapshot(config.free_floor_gib, "fake-mac"))):
            answer = offload.ensure_mac_disk_floor(offload.ProcessController(), config,
                offload.KnownRepository("hana"), offload.ProbeSnapshot(0, "fake-mac"))
        self.assertEqual(answer, offload.MacDiskReady(config.free_floor_gib))
        self.step(state=step_record.StepState.ON_MAC)
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(self.call))
        mac_idle.watch_once(self.paths, 1601, 101)
        saved = mac_idle.read_record(self.paths.state / "alert.json")
        self.assertEqual(saved["waiting"], 0)
        self.assertNotEqual(saved["cause"], "disk")
        self.assertIsNone(mac_idle_alert.alert_line(
            self.paths.state / "alert.json", 1601, ZoneInfo("UTC")))

    def test_admission_query_failure_reports_reason_and_preserves_independent_checks(self) -> None:
        for blocked, reason in ((False, "no socket"), (True, "connect refused")):
            with self.subTest(blocked=blocked, reason=reason):
                if blocked:
                    self.block()
                (self.paths.state / "alert.json").unlink(missing_ok=True)
                with mock.patch.object(admission_client, "view", side_effect=wire.AdmissionUnavailable(reason)), \
                     mock.patch.object(mac_idle, "notify_outage", side_effect=REAL_NOTIFY_OUTAGE), \
                     mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as notify, \
                     redirect_stderr(io.StringIO()) as errors:
                    mac_idle.watch_once(self.paths, 1180, 100)
                    mac_idle.watch_once(self.paths, 1240, 160)
                line = mac_idle_alert.alert_line(self.paths.state / "alert.json", 1240, ZoneInfo("UTC")) or ""
                self.assertIn("admission query failed", line)
                self.assertIn(reason, line)
                self.assertIn(reason, errors.getvalue())
                message = cast(str, notify.call_args_list[0].args[0])
                self.assertIn(reason, message)
                if not blocked:
                    self.assertEqual(mac_idle.marker_path(self.paths, self.call.call_id).read_text(), "retry_delayed\n")
                mac_idle.watch_once(self.paths, 1300, 220)
                self.assertNotIn("admission query failed", mac_idle_alert.alert_line(
                    self.paths.state / "alert.json", 1300, ZoneInfo("UTC")) or "")

    def test_admission_numeric_overflow_reports_query_failure_without_stopping_the_watcher(self) -> None:
        reply = wire.encode_reply(wire.BuildHistoryReply(10**400))
        with mock.patch.object(self.admission.server, "line_filter", return_value=reply), \
             redirect_stderr(io.StringIO()) as errors:
            mac_idle.watch_once(self.paths, 1180, 100)
            mac_idle.watch_once(self.paths, 1240, 160)
        line = mac_idle_alert.alert_line(self.paths.state / "alert.json", 1240, ZoneInfo("UTC")) or ""
        self.assertIn("admission query failed: invalid admission message", line)
        self.assertIn("admission query failed: invalid admission message", errors.getvalue())
        self.assertEqual(mac_idle.marker_path(self.paths, self.call.call_id).read_text(), "retry_delayed\n")
        mac_idle.watch_once(self.paths, 1300, 220)
        self.assertNotIn("admission query failed", mac_idle_alert.alert_line(
            self.paths.state / "alert.json", 1300, ZoneInfo("UTC")) or "")

    def test_unreachable_and_unreadable_readings_save_diagnostics_without_sends(self) -> None:
        for name, content, state in (("unreachable_until", "2000", "unreachable"),
                                     ("block.json", "{", "unreadable"), ("claims/bad.json", "{", "unreadable")):
            path = self.paths.mac_state / name
            path.parent.mkdir(exist_ok=True)
            _ = path.write_text(content)
            with self.subTest(name=name), mock.patch.object(mac_idle, "notify_outage") as notify, \
                 mock.patch.object(mac_idle, "push_outage") as push:
                mac_idle.watch_once(self.paths, 1180, 100)
                saved = mac_idle.read_record(self.paths.state / "alert.json")
                self.assertEqual(saved["state"], state)
                notify.assert_not_called()
                push.assert_not_called()
                self.assertTrue(mac_idle_alert.alert_line(self.paths.state / "alert.json", 1180, ZoneInfo("UTC")))
            path.unlink()

    def test_dead_process_claim_is_free_and_remote_claim_is_always_live(self) -> None:
        _ = self.claim(working=True, remote=False)
        self.assertIsInstance(mac_idle.mac_work(self.paths, 1180), mac_idle.FreeMac)
        _ = self.claim(working=True)
        self.assertIsInstance(mac_idle.mac_work(self.paths, 1180), mac_idle.NatedevJobOnMac)

    def test_cleared_reads_only_fresh_saved_result(self) -> None:
        with mock.patch.object(time, "time", return_value=1180):
            self.assertEqual(mac_idle.main(["cleared", "--since", "1000"]), 1)
            mac_idle.watch_once(self.paths, 1180, 100)
            self.assertEqual(mac_idle.main(["cleared", "--since", "1000"]), 1)
            self.assertEqual(mac_idle.main(["cleared", "--since", "999"]), 0)
            step_record.record_path(self.call.call_id).unlink(missing_ok=True)
            mac_idle.watch_once(self.paths, 1180, 100)
            self.assertEqual(mac_idle.main(["cleared", "--since", "1000"]), 0)
        with mock.patch.object(time, "time", return_value=1481):
            self.assertEqual(mac_idle.main(["cleared", "--since", "999"]), 1)

    def test_stuck_push_omits_reservation_sentence_only_when_mac_is_free(self) -> None:
        entry = next(entry for entry in catalog() if entry.id == "mac_idle.stuck")
        examples = {example.name: example.values for example in entry.examples}
        for holder in ("", "none"):
            with self.subTest(holder=holder):
                push = render_entry(entry, {**examples["free_mac"], "reservation": mac_idle_reservation(holder, "")})
                self.assertIn("7 steps wait.", push.body)
                self.assertIn("the builds showrunner checks why the waiting steps do not take the Mac", push.body)
                self.assertNotIn("reservation", push.body)
                self.assertNotIn("holder", push.body)
                self.assertNotIn("label", push.body)
        claim = render_entry(entry, examples["idle_research_claim"])
        self.assertIn('holder "research" and label "P15 F11 L0 run"', claim.body)

    def test_alert_boundary_validates_fields_and_line_is_fast_without_subprocess(self) -> None:
        self.block("research\u001b\n", "run\u0000")
        mac_idle.watch_once(self.paths, 1180, 100)
        path = self.paths.state / "alert.json"
        saved = mac_idle.read_record(path)
        started = time.monotonic()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("file read only")):
            line = mac_idle_alert.alert_line(path, 1180, ZoneInfo("UTC"))
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertIn("Blocked by research since 00:16 (run)", line or "")
        self.assertNotIn("\u001b", line or "")
        invalid_values: tuple[tuple[str, object], ...] = (("version", True), ("written", float("nan")), ("since", "1000"),
                               ("changed", None), ("changed", True), ("changed", "1000"), ("changed", -1),
                               ("changed", float("nan")), ("changed", float("inf")), ("changed", 10 ** 400),
                               ("waiting", True), ("state", "other"), ("cause", "other"), ("holder", 1),
                               ("detail", []), ("until", -1), ("until", 10 ** 400), ("messaged", 1), ("pushed", "no"))
        for field, invalid in invalid_values:
            with self.subTest(field=field):
                mac_idle.write_record(path, {**saved, field: invalid})
                self.assertIsInstance(mac_idle_alert.read_alert(path), mac_idle_alert.UnreadableAlert)
        mac_idle.write_record(path, saved)
        self.assertIn("no reading since", mac_idle_alert.alert_line(path, 1481, ZoneInfo("UTC")) or "")

    def test_idle_eligible_wait_gets_one_marker_and_one_row(self) -> None:
        with mock.patch.object(mac_idle, "notify_showrunner") as send:
            self.two_passes()
        send.assert_not_called()
        self.assertEqual(mac_idle.marker_path(self.paths, self.call.call_id).read_text(), "retry_delayed\n")
        self.assertEqual([(row["cause"], row["action"], row["wait"]) for row in self.rows()],
                         [("retry_delayed", "retry_marker", "memory")])
        self.assertEqual(len(self.admission.server.waiters), 1)

    def test_waiting_check_doc_and_lint_records_get_repairs_without_argv_selection(self) -> None:
        self.admission.end_fixture_owner(124)
        self.process(123, 1, "456", ("bash", "unrelated-name"))
        for kind, state in (("check", step_record.StepState.WAITING_HOLD),
                            ("doc", step_record.StepState.WAITING_MAC),
                            ("lint", step_record.StepState.WAITING_MAC)):
            with self.subTest(kind=kind):
                self.step(kind, state)
                with mock.patch("subprocess.run", side_effect=AssertionError("no eligibility subprocess")):
                    self.two_passes()
                self.assertEqual(mac_idle.marker_path(self.paths, self.call.call_id).read_text(), "retry_delayed\n")
                calls = mac_idle.waiting_calls(self.paths, mac_idle.processes(self.paths.proc))
                self.assertEqual([(call.kind, call.package, call.wait_state.value) for call in calls],
                                 [(kind, "", state.value)])
                self.assertEqual([(row["cause"], row["wait"], row["package"]) for row in self.rows()],
                                 [("retry_delayed", state.value, "")])
                mac_idle.call_path(self.paths, self.call.call_id).with_suffix(".cases").unlink()
                for path in (self.root / "buildlog").glob("*/*.jsonl"):
                    path.unlink()

    def test_natedev_only_and_running_records_do_not_trigger_repairs(self) -> None:
        for allowed, state in ((False, step_record.StepState.WAITING_HOLD),
                               (False, step_record.StepState.WAITING_MAC),
                               (True, step_record.StepState.ON_MAC),
                               (True, step_record.StepState.ON_NATEDEV)):
            with self.subTest(allowed=allowed, state=state):
                self.step("test", state, allowed)
                with mock.patch.object(mac_idle, "notify_showrunner") as send:
                    self.two_passes()
                send.assert_not_called()
                self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
                self.assertEqual(self.rows(), [])

    def test_linux_only_waits_and_linux_execution_never_trigger_mac_repairs(self) -> None:
        cases = (
            (step_record.AnyTarget(("aws",)), step_record.StepState.WAITING_HOLD, ""),
            (step_record.AnyTarget(("aws",)), step_record.StepState.WAITING_SSH, ""),
            (step_record.AnyTarget(("mac", "aws"), ("mac",)), step_record.StepState.ON_SSH, "aws"),
        )
        for permission, state, selected in cases:
            with self.subTest(state=state, selected=selected):
                step_record.write(step_record.StepRecord(
                    self.call.call_id, self.call.pid, self.call.start, Path(self.call.repo), "check",
                    step_record.WorkspacePackages(), permission, state, "1970-01-01T00:16:40Z",
                    selected_target=selected))
                self.assertIsInstance(step_record.read(self.call.call_id), step_record.StepRecord)
                with mock.patch.object(mac_idle, "notify_showrunner") as send:
                    self.two_passes()
                send.assert_not_called()
                self.assertEqual(mac_idle.waiting_calls(self.paths, mac_idle.processes(self.paths.proc)), ())
                self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
                self.assertEqual(self.rows(), [])

    def test_mixed_target_record_keeps_mac_wait_observation_after_decoding(self) -> None:
        step_record.write(step_record.StepRecord(
            self.call.call_id, self.call.pid, self.call.start, Path(self.call.repo), "test",
            step_record.NamedPackage("sample"), step_record.AnyTarget(("studio", "aws"), ("studio",)),
            step_record.StepState.WAITING_HOLD, "1970-01-01T00:16:40Z"))

        self.two_passes()

        self.assertEqual(mac_idle.marker_path(self.paths, self.call.call_id).read_text(), "retry_delayed\n")
        calls = mac_idle.waiting_calls(self.paths, mac_idle.processes(self.paths.proc))
        self.assertEqual([(call.call_id, call.package) for call in calls], [(self.call.call_id, "sample")])
        self.assertEqual([row["cause"] for row in self.rows()], ["retry_delayed"])

    def test_unreadable_and_stale_records_are_named_without_hiding_a_live_waiter(self) -> None:
        step_record.write(step_record.StepRecord("stale-call", 123, "old-start", Path(self.call.repo),
            "check", step_record.WorkspacePackages(), step_record.AnyTarget(("mac", "natedev"), ("mac",)), step_record.StepState.WAITING_MAC, "2026-10-28T22:00:00Z"))
        damaged = step_record.record_path("damaged-call")
        _ = damaged.write_text("{")
        errors = io.StringIO()
        with redirect_stderr(errors), mock.patch.object(mac_idle, "notify_showrunner") as send:
            self.two_passes()
        send.assert_not_called()
        self.assertIn("ignored stale step record stale-call", errors.getvalue())
        self.assertIn("ignored unreadable step record damaged-call", errors.getvalue())
        self.assertEqual([row["call_id"] for row in self.rows()], [self.call.call_id])
        self.assertEqual(mac_idle.read_record(self.paths.state / "idle.json"), {"since": 1000})
        self.assertTrue(damaged.exists())

    def test_verify_argv_and_retry_registration_without_a_step_record_do_not_trigger_repairs(self) -> None:
        step_record.record_path(self.call.call_id).unlink(missing_ok=True)
        with mock.patch.object(mac_idle, "notify_showrunner") as send:
            self.two_passes()
        send.assert_not_called()
        self.assertEqual(self.rows(), [])
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())

    def test_live_record_without_retry_registration_sends_one_message(self) -> None:
        mac_idle.call_path(self.paths, self.call.call_id).unlink()
        self.process(123, 1, "456", ("bash", "unrelated-name"))
        self.step("doc", step_record.StepState.WAITING_MAC)
        with mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as send:
            self.two_passes()
        send.assert_called_once()
        reported = cast(str, send.call_args.args[0])
        self.assertIn(f"Repo {self.call.repo}", reported)
        self.assertIn("verify.sh doc , pid 123", reported)
        self.assertEqual([(row["cause"], row["wait"]) for row in self.rows()],
                         [("call_has_no_marker_hook", "memory")])
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())

    def test_live_session_missing_temp_root_is_created(self) -> None:
        Path(self.call.temp_dir).rmdir()
        self.two_passes()
        self.assertTrue(Path(self.call.temp_dir).is_dir())
        self.assertEqual([row["cause"] for row in self.rows()], ["missing_temp_root", "retry_delayed"])

    def test_any_claim_stops_repairs(self) -> None:
        claims = self.paths.mac_state / "claims"
        claims.mkdir()
        claim = claims / "repo.json"
        _ = claim.write_text("claim must stay unchanged")
        self.two_passes()
        self.assertEqual(claim.read_text(), "claim must stay unchanged")
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
        self.assertEqual(self.rows(), [])

    def test_unknown_cause_sends_once_for_the_call(self) -> None:
        call = replace(self.call, retry_after=0)
        mac_idle.write_record(mac_idle.call_path(self.paths, call.call_id), asdict(call))
        with mock.patch.object(mac_idle, "notify_showrunner", return_value=True) as send:
            self.two_passes()
        send.assert_called_once_with(mac_idle.repair_message(
            replace(call, wait_state=step_record.StepState.WAITING_HOLD), "idle_with_waiter"))
        self.assertEqual([row["action"] for row in self.rows()], ["message_showrunner"])

    def test_failed_send_stays_one_case_until_it_is_accepted(self) -> None:
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(replace(self.call, retry_after=0)))
        with mock.patch.object(mac_idle, "notify_showrunner", side_effect=[False, True]) as send:
            self.two_passes()
            mac_idle.watch_once(self.paths, 1300, 200)
        self.assertEqual(send.call_count, 2)
        self.assertEqual([row["action"] for row in self.rows()], ["message_pending"])

    def test_missing_temp_root_is_not_created_for_an_ended_session(self) -> None:
        Path(self.call.temp_dir).rmdir()
        self.process(122, 1, "999", ("claude",))
        self.two_passes()
        self.assertFalse(Path(self.call.temp_dir).exists())
        self.assertEqual([row["cause"] for row in self.rows()], ["retry_delayed"])

    def test_hold_wait_without_explicit_hook_uses_marker(self) -> None:
        self.admission.end_fixture_owner(124)
        self.process(124, 123, "457", (sys.executable, "/scripts/build_targets.py", "wait-local",
            "--call-id", self.call.call_id), {"BUILDLOG_CALL_ID": self.call.call_id})
        mac_idle.write_record(Path(self.call.hold_waiter), {"pid": 124, "proc_start": "457", "call_id": self.call.call_id})
        mac_idle.write_record(mac_idle.call_path(self.paths, self.call.call_id), asdict(replace(self.call, retry_after=0)))
        self.two_passes()
        self.assertEqual([row["cause"] for row in self.rows()], ["missing_leave_hook"])
        self.assertTrue(mac_idle.marker_path(self.paths, self.call.call_id).exists())

    def test_token_wait_evidence_is_reported_for_a_live_waiting_record(self) -> None:
        self.admission.end_fixture_owner(124)
        token = Path(self.call.token_dir)
        token.mkdir()
        _ = (token / "impl.key.stamp.123").write_text("")
        self.two_passes()
        self.assertEqual([row["wait"] for row in self.rows()], ["cargo_token"])

    def test_pid_reuse_never_repairs_a_call_and_names_the_stale_record(self) -> None:
        self.process(123, 1, "999", ("bash", "/scripts/verify.sh", "test", "sample"))
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.two_passes()
        self.assertIn("ignored stale step record v-call", errors.getvalue())
        self.assertEqual(self.rows(), [])
        self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())

    def test_routing_wait_requires_a_child_with_the_call_id(self) -> None:
        self.admission.end_fixture_owner(124)
        self.process(124, 123, "457", (sys.executable, "/scripts/direct/local_release_wait.py", "15", self.call.call_id))
        self.two_passes()
        self.assertEqual([row["wait"] for row in self.rows()], ["routing"])

    def test_block_and_unreachable_backoff_stop_repairs(self) -> None:
        for name, text in (("block.json", "{}"), ("unreachable_until", "2000")):
            with self.subTest(name=name):
                path = self.paths.mac_state / name
                _ = path.write_text(text)
                self.two_passes()
                self.assertFalse(mac_idle.marker_path(self.paths, self.call.call_id).exists())
                self.assertEqual(self.rows(), [])
                self.assertEqual(path.read_text(), text)
                path.unlink()

    def test_three_minutes_start_when_idle_is_first_seen(self) -> None:
        (self.paths.state / "idle.json").unlink()
        mac_idle.watch_once(self.paths, 1000, 100)
        mac_idle.watch_once(self.paths, 1179, 100)
        self.assertEqual(self.rows(), [])
        mac_idle.watch_once(self.paths, 1180, 100)
        self.assertEqual(len(self.rows()), 1)

    def test_own_claim_work_restarts_the_idle_interval(self) -> None:
        _ = self.claim()
        with mock.patch.object(mac_idle, "notify_outage", return_value=True) as notify:
            mac_idle.watch_once(self.paths, 1180, 100)
            notify.assert_called_once()
            _ = self.claim(work_idle_since=1200)
            mac_idle.watch_once(self.paths, 1240, 160)
            self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["since"], 1200)
            self.assertEqual(notify.call_count, 1)
            mac_idle.watch_once(self.paths, 1380, 300)
            self.assertEqual(notify.call_count, 2)

    def test_other_repository_claim_changes_send_one_message_for_the_idle_episode(self) -> None:
        _ = self.claim()
        since = datetime(2026, 11, 5, 12, 50, tzinfo=ZoneInfo("America/New_York")).timestamp()
        mac_idle.write_record(self.paths.state / "idle.json", {"since": since})
        with self.outage_sender() as (fixture, push, run):
            mac_idle.watch_once(self.paths, since + 600, 100)
            other = self.claim(name="cargo-liner", since=since, working=True, remote=False)
            os.utime(other.parent, (since + 956, since + 956))
            mac_idle.watch_once(self.paths, since + 960, 460)
            other.unlink()
            os.utime(other.parent, (since + 1000, since + 1000))
            mac_idle.watch_once(self.paths, since + 1140, 640)
            self.assertEqual(fixture.count(), 1)
            push.assert_called_once()
        self.assertEqual(self.sent_keys(run), [f"mac-idle-showrunner-{int(since)}-{int(since)}"])
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["since"], since)

    def test_cause_and_holder_changes_send_new_messages_without_another_push(self) -> None:
        _ = self.claim()
        with self.outage_sender() as (fixture, push, run):
            mac_idle.watch_once(self.paths, 1600, 100)
            self.block()
            mac_idle.watch_once(self.paths, 1660, 160)
            mac_idle.watch_once(self.paths, 1720, 220)
            self.assertEqual(fixture.count(), 2)
            self.block("release")
            mac_idle.watch_once(self.paths, 1780, 280)
            mac_idle.watch_once(self.paths, 1840, 340)
            self.assertEqual(fixture.count(), 3)
            push.assert_called_once()
        self.assertEqual(self.sent_keys(run), [
            "mac-idle-showrunner-1000-1000", "mac-idle-showrunner-1000-1660",
            "mac-idle-showrunner-1000-1780"])
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["since"], 1000)

    def test_mac_work_ends_the_episode_and_new_idle_work_sends_a_new_message(self) -> None:
        _ = self.claim()
        with self.outage_sender() as (fixture, push, run):
            mac_idle.watch_once(self.paths, 1180, 100)
            _ = self.claim(working=True)
            mac_idle.watch_once(self.paths, 1240, 160)
            self.assertFalse((self.paths.state / "idle.json").exists())
            _ = self.claim(work_idle_since=1300)
            mac_idle.watch_once(self.paths, 1300, 220)
            mac_idle.watch_once(self.paths, 1480, 400)
            mac_idle.watch_once(self.paths, 1540, 460)
            self.assertEqual(fixture.count(), 2)
            push.assert_not_called()
        self.assertEqual(self.sent_keys(run), [
            "mac-idle-showrunner-1000-1000", "mac-idle-showrunner-1300-1300"])

    def test_blocked_episode_ends_when_a_live_claim_records_mac_work(self) -> None:
        _ = self.claim()
        self.block()
        with self.outage_sender() as (fixture, push, run):
            mac_idle.watch_once(self.paths, 1600, 100)
            _ = self.claim(work_idle_since=1700)
            mac_idle.watch_once(self.paths, 1740, 240)
            self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["since"], 1700)
            self.assertEqual(fixture.count(), 1)
            mac_idle.watch_once(self.paths, 1880, 380)
            mac_idle.watch_once(self.paths, 2300, 800)
            self.assertEqual(fixture.count(), 2)
            self.assertEqual(push.call_count, 2)
        self.assertEqual(self.sent_keys(run), [
            "mac-idle-showrunner-1000-1000", "mac-idle-showrunner-1700-1700"])

    def test_later_claim_that_sorts_first_keeps_the_idle_episode(self) -> None:
        _ = self.claim(since=1100, work_idle_since=1100)
        with self.outage_sender() as (fixture, push, run):
            mac_idle.watch_once(self.paths, 1600, 100)
            _ = self.claim(name="another-repository", since=1050, work_idle_since=1650)
            mac_idle.watch_once(self.paths, 1660, 160)
            mac_idle.watch_once(self.paths, 1720, 220)
            self.assertEqual(fixture.count(), 1)
            push.assert_called_once()
        self.assertEqual(self.sent_keys(run), ["mac-idle-showrunner-1000-1000"])
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["since"], 1000)

    def test_returning_cause_sends_a_new_message_in_the_same_episode(self) -> None:
        _ = self.claim()
        with self.outage_sender() as (fixture, push, run):
            mac_idle.watch_once(self.paths, 1600, 100)
            self.block()
            mac_idle.watch_once(self.paths, 1660, 160)
            (self.paths.mac_state / "block.json").unlink()
            mac_idle.watch_once(self.paths, 1720, 220)
            mac_idle.watch_once(self.paths, 1780, 280)
            self.assertEqual(fixture.count(), 3)
            push.assert_called_once()
        self.assertEqual(self.sent_keys(run), [
            "mac-idle-showrunner-1000-1000", "mac-idle-showrunner-1000-1660",
            "mac-idle-showrunner-1000-1720"])
        self.assertEqual(mac_idle.read_record(self.paths.state / "alert.json")["since"], 1000)

    def test_message_resolves_builds_showrunner_address(self) -> None:
        with mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess[str]([], 0, "uds:/tmp/builds.sock\n", ""),
            subprocess.CompletedProcess[str]([], 1, "QUEUED: socket", "")]) as run:
            self.assertTrue(mac_idle.notify_showrunner("Mac idle watch: idle_with_waiter"))
        self.assertEqual(run.call_args_list[0].args[0],
                         [str(mac_idle.SCRIPTS / "lib/py"), str(mac_idle.SCRIPTS / "production/showrunners.py"), "address", "builds"])
        command = cast(list[str], run.call_args_list[1].args[0])
        self.assertEqual(command[command.index("--to") + 1], "uds:/tmp/builds.sock")
        self.assertEqual(command[command.index("--text") + 1], "Mac idle watch: idle_with_waiter")
        self.assertNotIn("user", command)

    def test_cases_reach_buildlog_index_and_schema(self) -> None:
        self.two_passes()
        _ = index.update()
        with closing(sqlite3.connect(index.index_path())) as connection:
            self.assertEqual(connection.execute("SELECT call_id, cause, action FROM mac_idle").fetchall(),
                             [(self.call.call_id, "retry_delayed", "retry_marker")])

    def test_minute_sample_runs_the_watch_on_natedev(self) -> None:
        with mock.patch.object(sample, "sample", return_value=sample.SampleTaken(1000, "natedev")), \
             mock.patch.object(mac_idle, "watch_once") as watch:
            self.assertEqual(cli.main(["sample"]), 0)
        watch.assert_called_once()
        self.assertEqual(watch.call_args.args[1], 1000)

    def test_memory_wait_reads_the_marker_when_its_normal_hook_is_empty(self) -> None:
        self.admission.free(0)
        self.admission.configure(running_pass_seconds=0.01, wait_reply_seconds=0.05)
        marker = self.root / "retry"
        def publish_marker(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait):
                _ = marker.write_text("retry\n")
            return reply
        self.admission.server.reply_filter = publish_marker
        command = '''
set -euo pipefail
source "$1"
BUILDLOG_MAC_IDLE_HOOK='test -f "$TEST_RETRY_MARKER"'
LINT_QUEUE_LEAVE_HOOK=''
buildlog_wait_for_memory cargo nextest run
test "$BUILDLOG_MEM_OUTCOME" = LeftQueue
'''
        with tempfile.TemporaryFile(mode="w+") as output, tempfile.TemporaryFile(mode="w+") as errors:
            process = subprocess.Popen(["bash", "-c", command, "memory-marker-test",
                str(mac_idle.SCRIPTS / "lint/memory_gate.sh")], env={**os.environ,
                "TEST_RETRY_MARKER": str(marker)}, stdout=output, stderr=errors, start_new_session=True)
            expired = False
            try:
                _ = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                expired = True
            finally:
                try:
                    send_process_group(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    _ = process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.fail("the marker test process did not stop within 3 seconds after SIGKILL")
            _ = output.seek(0)
            _ = errors.seek(0)
            diagnostics = output.read() + errors.read()
        if expired:
            self.fail("the memory wait did not read the retry marker within 30 seconds\n" + diagnostics)
        self.assertEqual(process.returncode, 0, diagnostics)
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    _ = unittest.main()
