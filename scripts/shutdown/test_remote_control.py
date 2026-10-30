"""Remote Control repair uses only a proven idle session's own pane."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.lib import process_start as process_identity
from typing import Literal, TypedDict, cast, final, override
from unittest.mock import patch

from test_support import ShutdownSendLeaf, ShutdownTestCase
from test_restart import entry, showrunner, top_level, unit
import remote_control
import herdr
import sessions
import session_pane
import settle
import detached
from push_texts import RenderedPush
from herdr import HerdrPlace
from herdr import AgentBlocked


def unchanged(_at: float) -> None:
    pass


@final
class FakeClock:
    def __init__(self) -> None:
        self.at = 0.0
        self.tick: Callable[[float], None] = unchanged

    def monotonic(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        if seconds <= 0 or seconds > 0.5:
            raise AssertionError(f"unexpected poll duration {seconds}")
        self.at += seconds
        if self.at > 901:
            raise AssertionError("fixer exceeded its deadline")
        self.tick(self.at)


class RegistryFixture(TypedDict):
    pid: int
    procStart: str
    sessionId: str
    name: str
    messagingSocketPath: str
    updatedAt: int
    tmux: str
    formerNames: list[str]
    status: Literal["idle", "busy"]
    bridgeSessionId: str


@final
class RemoteControlTests(ShutdownTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.registry = Path()
        self.clock = FakeClock()
        self.place = HerdrPlace("owner", "w1:p7")
        self.typed: list[tuple[float, str]] = []
        self.screen = ""
        self.fields: RegistryFixture = {
            "pid": 321, "procStart": "123", "sessionId": "owner", "name": "Owner",
            "messagingSocketPath": "", "updatedAt": 1, "tmux": "owner:@1.%7",
            "formerNames": [], "status": "idle", "bridgeSessionId": "",
        }

    @override
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.registry = self.root / "registry"
        self.registry.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "NOTIFIER_SESSIONS_DIR": str(self.registry),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
        })))
        _ = self.enterContext(patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(patch.object(process_identity, "process_start", return_value=process_identity.parse_start("123")))
        _ = self.enterContext(patch.object(settle, "local_machine", return_value="natedev"))
        self.clock = FakeClock()
        self.write()
        _ = self.enterContext(patch.object(session_pane, "record_place", side_effect=self.find_place))
        _ = self.enterContext(patch.object(session_pane, "type_place", side_effect=self.type_command))
        _ = self.enterContext(patch.object(session_pane, "read_place", side_effect=self.read_screen))

    def write(self) -> None:
        _ = (self.registry / "321.json").write_text(json.dumps(self.fields), encoding="utf-8")

    def find_place(self, saved: sessions.SessionRecord) -> HerdrPlace:
        self.assertEqual(saved["sessionId"], "owner")
        self.assertEqual(saved["pid"], 321, "fixer must resolve the confirmed process's own pane")
        self.assertEqual(saved.get("procStart"), "123")
        return self.place

    def read_screen(self, _place: HerdrPlace) -> str:
        return self.screen

    def type_command(self, place: HerdrPlace, text: str) -> None:
        self.assertEqual(place, self.place)
        current = cast(dict[str, object], json.loads((self.registry / "321.json").read_text()))
        self.assertEqual(current["status"], "idle", "busy registry must never authorize input")
        self.assertEqual(current["procStart"], "123", "replacement must never receive input")
        self.typed.append((self.clock.at, text))

    def run_fix(self) -> tuple[remote_control.RemoteControlOutcome, str, list[RenderedPush]]:
        output = io.StringIO()
        with patch.object(settle, "alert_user") as alert, redirect_stdout(output):
            outcome = remote_control.fix("owner", 321, "123", "trunk unit", "Owner", clock=self.clock)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, output.getvalue())
        self.assertTrue(lines[0].startswith("trunk unit:"))
        return outcome, output.getvalue(), [cast(RenderedPush, call.args[0]) for call in alert.call_args_list]

    def test_launch_flag_activates_during_grace_without_pane_input_or_push(self) -> None:
        def activate(at: float) -> None:
            if at >= 14.5:
                self.fields["bridgeSessionId"] = "bridge"
                self.write()
        self.clock.tick = activate
        with patch.object(session_pane, "record_place", side_effect=AssertionError("flag grace inspected a pane")):
            outcome, text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.AlreadyOn)
        self.assertEqual(self.clock.at, 14.5)
        self.assertIn("Remote Control", text)
        self.assertEqual(self.typed, [])
        self.assertEqual(pushes, [])

    def test_registry_rewrite_after_read_cannot_mix_activation_and_status_snapshots(self) -> None:
        self.fields["bridgeSessionId"] = "bridge"
        self.write()
        read_text = Path.read_text
        reads: list[str] = []
        def read_then_rewrite(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            text = read_text(path, encoding=encoding, errors=errors)
            if path == self.registry / "321.json":
                reads.append(text)
                self.fields["status"] = "busy"
                self.fields["bridgeSessionId"] = ""
                self.write()
            return text
        with patch.object(Path, "read_text", new=read_then_rewrite):
            outcome, _text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.AlreadyOn)
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.clock.at, 0)
        self.assertEqual(self.typed, [])
        self.assertEqual(pushes, [])

    def test_idle_pane_command_enables_control_once_by_registry_or_screen(self) -> None:
        for evidence in ("registry", "screen"):
            with self.subTest(evidence=evidence):
                self.clock = FakeClock()
                self.fields["bridgeSessionId"] = ""
                self.screen = ""
                self.typed.clear()
                self.write()
                def activate(at: float) -> None:
                    if self.typed and at >= self.typed[0][0] + 1:
                        if evidence == "registry":
                            self.fields["bridgeSessionId"] = "bridge"
                            self.write()
                        else:
                            self.screen = "/remote-control is active\n"
                self.clock.tick = activate
                outcome, text, pushes = self.run_fix()
                self.assertIsInstance(outcome, remote_control.TurnedOn)
                self.assertEqual(self.typed, [(60.0, "/remote-control")])
                self.assertEqual(self.clock.at, 61.0)
                self.assertIn("Remote Control on", text)
                self.assertEqual(pushes, [])

    def test_busy_then_idle_registry_delays_input_until_idle(self) -> None:
        self.fields["status"] = "busy"
        self.write()
        def become_idle(at: float) -> None:
            if at >= 75:
                self.fields["status"] = "idle"
            if self.typed:
                self.fields["bridgeSessionId"] = "bridge"
            self.write()
        self.clock.tick = become_idle
        outcome, _text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.TurnedOn)
        self.assertEqual(self.typed, [(75.0, "/remote-control")])
        self.assertEqual(pushes, [])

    def test_three_unsuccessful_attempts_only_journal_after_last_confirmation_wait(self) -> None:
        outcome, text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.Failed)
        self.assertEqual(self.typed, [(60.0, "/remote-control"), (90.0, "/remote-control"), (120.0, "/remote-control")])
        self.assertEqual(self.clock.at, 150.0)
        self.assertIn("Remote Control failed", text)
        self.assertEqual(pushes, [])

    def test_missing_pane_waits_for_launch_flag_without_typing_or_pushing(self) -> None:
        with patch.object(session_pane, "record_place", side_effect=session_pane.NoSuchTarget("owner")):
            outcome, _text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.Failed)
        if isinstance(outcome, remote_control.Failed):
            self.assertEqual(outcome.reason, "no pane to type into")
        self.assertEqual(self.clock.at, 60.0)
        self.assertEqual(self.typed, [])
        self.assertEqual(pushes, [])

    def test_replaced_process_or_registry_ends_without_typing_or_push(self) -> None:
        for replacement in ("process", "registry"):
            with self.subTest(replacement=replacement):
                self.clock = FakeClock()
                self.fields["procStart"] = "123"
                self.write()
                def replace(at: float) -> None:
                    if replacement == "registry" and at >= 10:
                        self.fields["procStart"] = "replacement"
                        self.write()
                self.clock.tick = replace
                def observed_start(pid: int, proc_root: Path) -> process_identity.ProcessStartTime:
                    del pid, proc_root
                    return process_identity.ProcessStartTime(456 if replacement == "process" and self.clock.at >= 10 else 123)
                with patch.object(process_identity, "process_start", side_effect=observed_start):
                    outcome, _text, pushes = self.run_fix()
                self.assertIsInstance(outcome, remote_control.SessionGone)
                self.assertEqual(self.clock.at, 10.0)
                self.assertEqual(self.typed, [])
                self.assertEqual(pushes, [])

    def test_identity_changed_while_resolving_pane_is_rechecked_before_typing(self) -> None:
        def replace_before_input(saved: sessions.SessionRecord) -> HerdrPlace:
            place = self.find_place(saved)
            self.fields["sessionId"] = "different-session"
            self.write()
            return place
        with patch.object(session_pane, "record_place", side_effect=replace_before_input):
            outcome, _text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.SessionGone)
        self.assertEqual(self.clock.at, 60.0)
        self.assertEqual(self.typed, [])
        self.assertEqual(pushes, [])

    def test_duplicate_session_id_routes_input_to_the_confirmed_pid(self) -> None:
        newer: RegistryFixture = {**self.fields, "pid": 999, "procStart": "newer",
            "updatedAt": 2, "status": "busy", "tmux": "newer:@2.%99"}
        _ = (self.registry / "999.json").write_text(json.dumps(newer))
        resolved: list[int] = []
        def pane_for_record(saved: sessions.SessionRecord) -> HerdrPlace:
            resolved.append(saved["pid"])
            return HerdrPlace("owner", "w1:p7" if saved["pid"] == 321 else "w1:p99")
        def activate(at: float) -> None:
            if self.typed and at > self.typed[0][0]:
                self.fields["bridgeSessionId"] = "bridge"
                self.write()
        self.clock.tick = activate
        with (patch.object(sys, "platform", "linux"),
              patch.object(session_pane, "live_session", return_value=True),
              patch.object(session_pane, "record_place", side_effect=pane_for_record)):
            outcome, _text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.TurnedOn)
        self.assertTrue(resolved)
        self.assertEqual(set(resolved), {321})
        self.assertEqual(self.typed, [(60.0, "/remote-control")])
        self.assertEqual(pushes, [])

    def test_activation_during_failed_pane_lookup_finishes_without_push(self) -> None:
        for already_typed in (False, True):
            with self.subTest(already_typed=already_typed):
                self.clock = FakeClock()
                self.typed.clear()
                self.fields["bridgeSessionId"] = ""
                self.write()
                def disappearing_pane(saved: sessions.SessionRecord) -> HerdrPlace:
                    if already_typed and not self.typed:
                        return self.find_place(saved)
                    self.fields["bridgeSessionId"] = "bridge"
                    self.write()
                    raise session_pane.NoSuchTarget("owner")
                with patch.object(session_pane, "record_place", side_effect=disappearing_pane):
                    outcome, _text, pushes = self.run_fix()
                self.assertIsInstance(outcome, remote_control.TurnedOn if already_typed else remote_control.AlreadyOn)
                self.assertEqual(len(self.typed), int(already_typed))
                self.assertEqual(pushes, [])

    def test_transient_input_failure_retries_and_exhausted_input_failures_never_push(self) -> None:
        for recover in (True, False):
            with self.subTest(recover=recover):
                self.clock = FakeClock()
                self.typed.clear()
                self.fields["bridgeSessionId"] = ""
                self.write()
                attempted: list[float] = []
                def input_with_failure(place: HerdrPlace, text: str) -> None:
                    attempted.append(self.clock.at)
                    if not recover or len(attempted) == 1:
                        raise OSError("temporary pane transport failure")
                    self.type_command(place, text)
                    self.fields["bridgeSessionId"] = "bridge"
                    self.write()
                with patch.object(session_pane, "type_place", side_effect=input_with_failure):
                    outcome, _text, pushes = self.run_fix()
                self.assertEqual(len(attempted), 2 if recover else 3)
                self.assertIsInstance(outcome, remote_control.TurnedOn if recover else remote_control.Failed)
                self.assertEqual(len(self.typed), int(recover))
                self.assertEqual(pushes, [])

    def test_blocked_herdr_input_is_not_counted_as_a_successful_attempt(self) -> None:
        def blocked(_place: object, _text: str) -> None:
            raise AgentBlocked("busy")
        with patch.object(session_pane, "type_place", side_effect=blocked):
            outcome, _text, pushes = self.run_fix()
        self.assertIsInstance(outcome, remote_control.Failed)
        self.assertEqual(self.clock.at, 900.0)
        self.assertEqual(self.typed, [])
        self.assertEqual(pushes, [])

    def test_who_names_the_unit_production_or_plain_session(self) -> None:
        for saved, expected in ((entry(unit("trunk")), "trunk unit"),
                                (entry(showrunner("director")), "demo showrunner"),
                                (entry(top_level("plain", name="Workspace")), "Workspace")):
            with self.subTest(expected=expected):
                self.assertEqual(remote_control.remote_control_who(saved), expected)

    def test_detached_launch_carries_process_identity_unbuffered_python_and_who(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER)
        with patch.object(sys, "platform", "linux"), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as launch:
            remote_control.launch_fixer("owner-identifier", 321, "original start", "trunk unit", "Owner")
        launch.assert_called_once()
        argv = cast(list[str], launch.call_args.args[0])
        self.assertEqual(argv[0], "systemd-run")
        env = argv.index("env")
        self.assertEqual(argv[env:], ["env", "PYTHONUNBUFFERED=1", sys.executable, "-u",
            str(Path(remote_control.__file__).with_name("shutdown.py")), "remote-control", "owner-identifier",
            "--pid", "321", "--proc-start", "original start", "--who", "trunk unit", "--session", "Owner"])
        service = argv[argv.index("--unit") + 1]
        self.assertEqual(service, "shutdown-remote-control-owner-identi-321")
        self.assertTrue({"--user", "--collect", "--quiet", "--no-block"}.issubset(argv))

    def test_darwin_fixer_launch_waits_for_its_start_line_and_preserves_process_identity(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER)
        paths: list[Path] = []

        def start(argv: Sequence[str], *, ready: Callable[[], bool], within: float,
                  output: detached.AppendLog, environment: detached.ExplicitEnvironment) -> int:
            log = output.path
            self.assertEqual(environment.mapping[detached.STARTUP_SIGNAL_ENV], "1")
            self.assertEqual(list(argv), ["env", "PYTHONUNBUFFERED=1", sys.executable, "-u",
                str(Path(remote_control.__file__).with_name("shutdown.py")), "remote-control", "owner-identifier",
                "--pid", "321", "--proc-start", "original start", "--who", "trunk unit", "--session", "Owner"])
            self.assertEqual(within, 15)
            self.assertEqual(log.parent, self.root / "shutdown" / "logs")
            self.assertNotIn(log, paths)
            paths.append(log)
            self.assertFalse(ready())
            _ = log.write_text("unrelated output\n")
            self.assertFalse(ready())
            def already_on(session_id: str, pid: int, proc_start: str,
                           clock: remote_control.Clock) -> remote_control.AlreadyOn:
                del session_id, pid, proc_start, clock
                self.assertNotIn(detached.STARTUP_SIGNAL_ENV, os.environ)
                return remote_control.AlreadyOn()

            with (log.open("a") as stream, redirect_stderr(stream), redirect_stdout(io.StringIO()),
                  patch.dict(os.environ, environment.mapping, clear=True),
                  patch.object(remote_control, "_fix", side_effect=already_on)):
                _ = remote_control.fix("owner-identifier", 321, "original start", "trunk unit", "Owner")
                self.assertNotIn(detached.STARTUP_SIGNAL_ENV, os.environ)
            self.assertTrue(ready(), "the child entry point must publish its startup line")
            return 4321

        with (patch.object(sys, "platform", "darwin"), patch.object(herdr, "_PLATFORM", "darwin"),
              patch.object(detached, "start_detached", side_effect=start),
              patch.object(subprocess, "run", side_effect=AssertionError("service manager used"))):
            for _ in range(2):
                remote_control.launch_fixer("owner-identifier", 321, "original start", "trunk unit", "Owner")
        self.assertEqual(len(paths), 2)

    def test_plain_darwin_fixer_does_not_print_a_detached_startup_line(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != detached.STARTUP_SIGNAL_ENV}
        with (patch.object(sys, "platform", "darwin"), patch.object(herdr, "_PLATFORM", "darwin"),
              patch.dict(os.environ, environment, clear=True),
              patch.object(remote_control, "_fix", return_value=remote_control.AlreadyOn()),
              redirect_stderr(io.StringIO()) as error, redirect_stdout(io.StringIO()) as output):
            _ = remote_control.fix("id", 321, "start", "Owner", "Owner")
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(output.getvalue(), "Owner: Remote Control already on\n")

    def test_darwin_fixer_failed_child_reports_service_and_keeps_its_log(self) -> None:
        self.allow_leaf(ShutdownSendLeaf.LAUNCH_REMOTE_CONTROL_FIXER)
        paths: list[Path] = []

        def fail(argv: Sequence[str], *, ready: Callable[[], bool], within: float,
                  output: detached.AppendLog, environment: detached.ExplicitEnvironment) -> int:
            log = output.path
            self.assertEqual(environment.mapping[detached.STARTUP_SIGNAL_ENV], "1")
            del argv, within
            self.assertFalse(ready())
            paths.append(log)
            _ = log.write_text("child import failed\n")
            raise detached.DetachedStartFailure("child exited with status 1")

        with (patch.object(sys, "platform", "darwin"), patch.object(herdr, "_PLATFORM", "darwin"),
              patch.object(detached, "start_detached", side_effect=fail),
              patch.object(subprocess, "run", side_effect=AssertionError("service manager used")),
              self.assertRaisesRegex(RuntimeError,
                  "could not start Remote Control fixer shutdown-remote-control-owner-identi-321: child exited with status 1") as failure):
            remote_control.launch_fixer("owner-identifier", 321, "original start", "trunk unit", "Owner")
        self.assertEqual(paths[0].read_text(), "child import failed\n")
        self.assertIn(str(paths[0]), str(failure.exception))
