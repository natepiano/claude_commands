"""The client's three failure outcomes keep episode identity and time bounds."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import errno
import io
import json
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
import sys
import unittest
from unittest import mock

from scripts.accounts import outage_alerts
from scripts.message import send_result
from scripts.message import roster
from scripts.message.session_send import session_send_argv
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.message.codex_test_support import FakeDaemon, receive_frame, reply, write_codex_account
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_worker, fixture_runtime_id, isolated_staff_file
from typing import cast
from scripts.lib import conf
from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import admission_records as records, queue_evidence, step_estimator
from scripts.build_queue.admission_test_support import admitted_reply, wait_reply
from scripts.build_queue.admission_test_support import (
    AdmissionFixture, Exchange, GIB, ScriptedTransport, ScriptSocket, TimedChunk, only_admitted, reply_bytes,
)
from scripts.build_queue.queue_fixture import QueueFixture, write_memory_config
from scripts.lint.memory_test_support import commit_config_fixture, config_environment

WAIT = wait_reply(records.NeedsMemory(), 1, "head", "free memory")
EXPIRED = wait_reply(records.EstimateExpired(1), 1, "head", "estimate expired")
GRANTED = admitted_reply("stem", GIB, "new-source", "stem.max", "stem.wave")


def setUpModule() -> None:
    forbid_real_sender()


class ClientTests(unittest.TestCase):
    def test_history_generation_reads_current_configuration_at_use(self) -> None:
        with AdmissionFixture() as fixture:
            transport = self.use_transport(fixture, Exchange(reply_bytes(wire.BuildHistoryReply(7))))
            with mock.patch.object(records, "read_memory_config", wraps=records.read_memory_config) as read:
                generation = client.build_history_generation(client.READ_CONFIGURATION_AT_USE)
            self.assertEqual(generation, records.BuildHistoryGeneration(7))
            self.assertEqual(len(transport.requests), 1)
            self.assertIsInstance(transport.requests[0], wire.BuildHistoryRequest)
            read.assert_called_once()

    def test_history_generation_uses_supplied_configuration_without_another_read(self) -> None:
        with AdmissionFixture() as fixture:
            configuration = records.read_memory_config()
            transport = self.use_transport(fixture, Exchange(reply_bytes(wire.BuildHistoryReply(8))))
            with mock.patch.object(records, "read_memory_config", side_effect=AssertionError("supplied configuration was not used")):
                generation = client.build_history_generation(configuration)
            self.assertEqual(generation, records.BuildHistoryGeneration(8))
            self.assertEqual(len(transport.requests), 1)
            self.assertIsInstance(transport.requests[0], wire.BuildHistoryRequest)

    def test_history_generation_refuses_an_unusable_current_configuration(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", silence_seconds="invalid")
            with redirect_stderr(io.StringIO()), self.assertRaises(client.CheckerFailure) as error:
                _ = client.build_history_generation(client.READ_CONFIGURATION_AT_USE)
            self.assertIn("silence_seconds", str(error.exception))

    def test_cloud_admit_uses_the_private_service_and_releases_its_slot(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = replace(fixture.request(step="merge-gate"), place=wire.CloudPlace("aws"))
            with fixture.serving():
                result = client.admit(request, lambda _wait: None, lambda: request)
                assert isinstance(result, wire.Admitted)
                self.assertEqual(result.need, 0)
                self.assertEqual(client.release(result.reservation, 0, result.cleanup_timing), wire.Ok())
            self.assertTrue(fixture.callers_released())

    def test_cloud_place_survives_client_reconnect_and_estimate_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            request = replace(fixture.request(step="merge-gate"), place=wire.CloudPlace("aws"))
            transport = self.use_transport(fixture,
                Exchange(reply_bytes(EXPIRED), TimedChunk(0.1, b"")),
                Exchange(reply_bytes(GRANTED)))
            refreshed = replace(request, place=wire.LocalPlace())
            with redirect_stderr(io.StringIO()):
                result = client.admit(request, lambda _wait: None, lambda: refreshed)
            self.assertEqual(result, GRANTED)
            self.assertGreaterEqual(len(transport.requests), 2)
            for sent in transport.requests:
                assert isinstance(sent, wire.AdmitRequest)
                self.assertEqual(sent.place, wire.CloudPlace("aws"))

    def test_import_does_not_load_memory_admission(self) -> None:
        result = subprocess.run([sys.executable, "-c",
            "import sys\nfrom scripts.build_queue import admission_client\nprint('\\n'.join(sys.modules))"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True)
        self.assertNotIn("scripts.lint.memory_admit", result.stdout.splitlines())

    def test_outage_message_reaches_builds_without_herdr_on_path(self) -> None:
        with short_socket_directory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            for name in ("ps", "ssh"):
                executable = binaries / name
                _ = executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            home = root / "home"
            daemon_home = home / ".codex"
            notes = root / "notes"
            write_codex_account(daemon_home, notes, "codex1")
            address = daemon_home / "app-server-control/control.sock"
            address.parent.mkdir()
            sessions = root / "sessions"
            sessions.mkdir()
            identity = fixture_runtime_id("outage-builds")
            environment = {"HOME": str(home), "PATH": str(binaries), "CLAUDE_PYTHON": sys.executable,
                "AGENT_NOTES_DIR": str(notes), "ROSTER_CODEX_SOCKET": str(address),
                "NOTIFIER_SESSIONS_DIR": str(sessions), "NOTIFIER_STATE_DIR": str(root / "notifier"),
                "ROSTER_RUN_ROOT": str(root / "runs"), "HERDR_PROC": str(root / "proc"),
                "DESIGN_ROLES_STATE": str(root / "roles"), "XDG_STATE_HOME": str(root / "state"),
                **config_environment(root / "config")}
            direct_config = root / "config/direct.conf"
            _ = direct_config.write_text(direct_config.read_text())
            received: list[dict[str, object]] = []

            def receive(connection: socket.socket) -> None:
                initialize = receive_frame(connection)
                self.assertEqual(initialize["method"], "initialize")
                reply(connection, initialize, {})
                self.assertEqual(receive_frame(connection)["method"], "initialized")
                while True:
                    try:
                        request = receive_frame(connection)
                    except EOFError:
                        return
                    if request["method"] == "thread/loaded/list":
                        reply(connection, request, {"data": [identity]})
                    elif request["method"] == "thread/read":
                        reply(connection, request, {"thread": {"id": identity, "name": "builds",
                            "cwd": str(root), "status": {"type": "idle"}}})
                    elif request["method"] == "thread/queue/add":
                        received.append(cast(dict[str, object], request["params"]))
                        reply(connection, request, {"queuedSubmission": {"id": "outage"}})
                    else:
                        self.fail(f"unexpected daemon method: {request['method']}")

            with mock.patch.dict(os.environ, environment, clear=True), \
                 isolated_staff_file(root / "staff.json", environment):
                document = root / "production.md"
                _ = document.write_text("# Builds fixture\n")
                runner = fixture_member("showrunner", name="builds", runtime_id=identity,
                    doc=staff.LiveDoc(roster.remote.local_machine(), document),
                    backend=staff.CodexBackend(daemon_home))
                parent = fixture_member("unit", name="pane-parent", parent=runner.id,
                    worktree=staff.PresentWorktree(roster.remote.local_machine(), root))
                _ = fixture_worker("reader", parent.id)
                command = session_send_argv(client.BUILDS_SESSION, sender="build-admission",
                    lint_settings=conf.read("lint"))
                child_environment = dict(os.environ)
                _ = child_environment.pop("TEST_SENDER_GUARD", None)
                _ = child_environment.pop("HERDR_BIN", None)
                child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
                message = "The build queue service has not answered. Builds wait."
                with FakeDaemon(address, receive, repeat=True):
                    result = subprocess.run(command, input=message, text=True, capture_output=True,
                        env=child_environment, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outcome = send_result.send_outcome(result.returncode, result.stdout)
            self.assertIsInstance(outcome, send_result.ConfirmedSendOutcome)
            assert isinstance(outcome, send_result.ConfirmedSendOutcome)
            self.assertEqual(outcome.header.outcome, "sent")
            self.assertEqual([(item["threadId"], item["input"]) for item in received],
                [(identity, [{"type": "text", "text": message + "\nSender native id: unavailable"}])])

    def test_runner_width_change_uses_its_account_and_changes_its_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            request = replace(fixture.request(owner=owner), episode=f"uid:{uid}:width-owner")
            with fixture.peer_account(uid):
                grant = only_admitted(fixture.server.handle_request(request))
                with fixture.serving(), mock.patch.object(client, "os", wraps=os) as runner_os, \
                     mock.patch.object(runner_os, "getuid", return_value=uid):
                    self.assertEqual(client.width(grant.reservation, 3), wire.Ok())
                self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(3))
            fixture.release(grant)


    def test_runner_closed_connections_reach_the_acquisition_deadline_without_home_reads(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.service_request()
            transport = self.use_transport(fixture,
                *(Exchange(TimedChunk(0.125, b"")) for _ in range(8)))
            with mock.patch.object(records, "read_memory_config", side_effect=PermissionError("runner home")), \
                 mock.patch.object(step_estimator, "step_estimate", side_effect=PermissionError("runner history")), \
                 redirect_stderr(io.StringIO()) as output:
                result = client.service_admit(request, lambda _wait: None,
                    wire.AcquisitionTiming(1, 0.1, 0.3))
            self.assertIsInstance(result, wire.Refused)
            self.assertIn("build queue unreachable for 0.3 s", output.getvalue())
            self.assertGreaterEqual(fixture.clock.monotonic(), 0.3)
            self.assertGreater(len(transport.requests), 1)
            self.assertEqual(transport.requests, [request] * len(transport.requests))

    def test_runner_wait_recovers_and_cleanup_uses_its_own_timing_without_home_reads(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.service_request()
            granted = replace(GRANTED, cleanup_timing=wire.CleanupTiming(7, 2))
            transport = self.use_transport(fixture,
                Exchange(TimedChunk(0.1, b"")),
                Exchange(TimedChunk(0.1, reply_bytes(WAIT)), TimedChunk(0.2, b"")),
                Exchange(reply_bytes(granted)), Exchange(reply_bytes(wire.Ok())))
            waits: list[wire.Wait] = []
            with mock.patch.object(records, "read_memory_config", side_effect=PermissionError("runner home")), \
                 mock.patch.object(step_estimator, "step_estimate", side_effect=PermissionError("runner history")), \
                 redirect_stderr(io.StringIO()) as output:
                result = client.service_admit(request, waits.append, wire.AcquisitionTiming(1, 0.1, 0.3))
                self.assertEqual(result, granted)
                self.assertEqual(client.release(granted.reservation, 137, granted.cleanup_timing), wire.Ok())
            self.assertEqual(waits, [WAIT])
            self.assertEqual(transport.requests[:3], [request] * 3)
            release = transport.requests[-1]
            self.assertIsInstance(release, wire.ReleaseRequest)
            if isinstance(release, wire.ReleaseRequest):
                self.assertEqual((release.reservation, release.status), (granted.reservation, 137))
            self.assertEqual(transport.timeouts[-1], 7)
            self.assertNotIn("step stopped", output.getvalue())

    def test_wait_events_name_each_holder_change_without_repeating_an_unchanged_holder(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            holders: tuple[records.WaitHolder, ...] = (
                records.SessionHolder("first-id", "first-name"),
                records.SessionHolder("first-id", "first-name"),
                records.SessionHolder("second-id", "second-name"),
                records.UnitHolder("compiler"), records.NoHolder())
            waits = [replace(WAIT, snapshot=replace(WAIT.snapshot, holder=holder)) for holder in holders]
            interval = records.read_memory_config(report=False).wait_reply_seconds
            events = [TimedChunk(interval + 1, reply_bytes(waits[1])),
                      *(reply_bytes(wait) for wait in waits[2:]), reply_bytes(GRANTED)]
            transport = self.use_transport(fixture, Exchange(reply_bytes(waits[0]), *events))
            with mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                    request.profile, step_estimator.ReusableEstimate())), \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as output:
                status = client.main(["admit", "--pid", str(request.owner.pid), "--wait-events", "--", "cargo", "check"])
            self.assertEqual(status, 0)
            lines = output.getvalue().splitlines()
            self.assertEqual((lines[0], lines[-1]), ("first", "done"))
            self.assertTrue(all(line.startswith("holder ") for line in lines[1:-1]))
            self.assertEqual([json.loads(line.removeprefix("holder ")) for line in lines[1:-1]], [
                {"kind": "session", "session_id": "first-id", "session_name": "first-name"},
                {"kind": "session", "session_id": "second-id", "session_name": "second-name"},
                {"kind": "unit", "unit": "compiler"}, {"kind": "none"}])
            self.assertEqual(len(transport.requests), 1)

    def test_a_wait_reply_without_a_holder_keeps_the_wait_and_reports_no_holder(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            record = cast(dict[str, object], json.loads(wire.encode_reply(WAIT)))
            data_record = cast(dict[str, object], record["data"])
            snapshot = cast(dict[str, object], data_record["snapshot"])
            del snapshot["holder"]
            data = (json.dumps(record) + "\n").encode()
            transport = self.use_transport(fixture, Exchange(data, reply_bytes(GRANTED)))
            with mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                    request.profile, step_estimator.ReusableEstimate())), \
                 redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as output:
                status = client.main(["admit", "--pid", str(request.owner.pid), "--wait-events", "--", "cargo", "check"])
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue().splitlines(), ["first", 'holder {"kind":"none"}', "done"])
            self.assertEqual(len(transport.requests), 1)

    def test_unit_override_uses_the_shared_request_identity(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            for unit in ("queue-unit", ""):
                with self.subTest(unit=unit), mock.patch.dict(os.environ, {records.BUILDLOG_UNIT: unit}):
                    request = client.step_request(list(original.argv), original.cwd, original.owner,
                        original.since, original.sidecar, original.episode, original.profile)
                    self.assertEqual(request.identity.unit, unit)
                    launch = wire.LaunchRequest(request.episode, request.owner, request.since,
                                                request.identity, request.profile)
                    self.assertEqual(launch.identity.unit, unit)
                    self.assertEqual(records.waiter_identity("nextest", "tree").get("unit", ""), unit)

    def use_transport(self, fixture: AdmissionFixture, *exchanges: Exchange) -> ScriptedTransport:
        transport = ScriptedTransport(fixture.clock, *exchanges)
        _ = self.enterContext(mock.patch.object(client, "CLOCK", fixture.clock))
        _ = self.enterContext(mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket))
        return transport

    def test_priority_commands_from_any_session_send_the_actor(self) -> None:
        for claude, codex, actor in (("ordinary-session", "codex-session", "ordinary-session"),
                                      ("", "codex-session", "codex-session"), ("", "", "user")):
            for argv in (("priority-set", "unit"), ("priority-clear",), ("admit-head", "urgent")):
                with self.subTest(actor=actor, argv=argv), \
                     mock.patch.dict(os.environ), \
                     mock.patch.object(client, "_mutation", return_value=wire.Ok()) as mutation, \
                     redirect_stdout(io.StringIO()):
                    for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
                        _ = os.environ.pop(key, None)
                    if claude:
                        os.environ["CLAUDE_CODE_SESSION_ID"] = claude
                    if codex:
                        os.environ["CODEX_THREAD_ID"] = codex
                    self.assertEqual(client.main(list(argv)), 0)
                    mutation.assert_called_once()
                    request = cast(wire.PrioritySetRequest | wire.PriorityClearRequest | wire.AdmitHeadRequest,
                                   mutation.call_args.args[0])
                    self.assertIsInstance(request, (wire.PrioritySetRequest, wire.PriorityClearRequest, wire.AdmitHeadRequest))
                    self.assertEqual(request.actor, actor)

    def test_unusable_config_exits_with_checker_failure_and_names_the_key(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="invalid")
            with fixture.serving(), redirect_stderr(io.StringIO()) as errors:
                status = client.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "nextest"])
            self.assertEqual(status, 2)
            self.assertIn(str(config), errors.getvalue())
            self.assertIn("estimate_refresh_seconds", errors.getvalue())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            self.assertFalse(fixture.server.reservations)


    def test_recorded_queue_replies_pass_the_client_query_paths(self) -> None:
        with AdmissionFixture() as fixture, QueueFixture() as queue:
            reading_reply = queue.read()
            view_reply = reply_bytes(wire.ViewReply(reading_reply))
            explain_reply = reply_bytes(wire.ExplainReply(reading_reply.explanation))
            transport = self.use_transport(fixture, Exchange(view_reply), Exchange(explain_reply))
            reading = client.view()
            explanation = client.explain()
            self.assertTrue(reading.view.running)
            self.assertTrue(explanation.running)
            self.assertGreater(explanation.totals.measured, 0)
            self.assertEqual(transport.requests, [wire.ViewRequest(code_version=client.LOADED_ADMISSION_CODE_VERSION),
                                                  wire.ExplainRequest(code_version=client.LOADED_ADMISSION_CODE_VERSION)])

    def test_recorded_pressure_snapshot_waits_then_admits_without_inventing_condition(self) -> None:
        pressure = records.MemoryPressure(8, 15, 80)
        captured = wait_reply(pressure)
        captured = replace(captured, snapshot=replace(captured.snapshot,
            running=tuple(records.RunningStep("fixture", "worker", "check", "/tree", use,
                records.MeasuredMemory(use), pid=index) for index, use in enumerate((4 * GIB, GIB), 1)),
            decision=replace(captured.snapshot.decision, pressure=pressure)))
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(captured), reply_bytes(GRANTED)))
            waits: list[wire.Wait] = []
            result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(transport.requests, [request])
            self.assertEqual(len(waits), 1)
            pressure = waits[0].snapshot.decision.pressure
            assert isinstance(pressure, records.MemoryPressure)
            self.assertIsInstance(pressure.condition, records.PressureConditionNotRecorded)
            self.assertEqual((pressure.full_avg10, pressure.some_avg10, pressure.swap_used_percent), (8, 15, 80))
            self.assertEqual(waits[0].reason, pressure)
            self.assertEqual([step.measured.bytes for step in waits[0].snapshot.running
                              if isinstance(step.measured, records.MeasuredMemory)], [4 * GIB, GIB])
            self.assertEqual(records.pressure_text(pressure),
                             "memory pressure: condition not recorded by service; full avg10 8.0; "
                             + "some avg10 15.0; swap 80.0% used")
            self.assertEqual(queue_evidence.pressure_text(pressure, waits[0].snapshot.policy),
                             "memory pressure: full avg10 8.0%, some avg10 15.0%, swap used 80.0%; "
                             + "condition not recorded by service")


    def test_hold_after_grant_releases_before_waiting_and_uses_a_new_episode(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            final = replace(GRANTED, reservation="second")
            transport = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)),
                Exchange(reply_bytes(wire.Ok())), Exchange(reply_bytes(final)))
            events: list[str] = []
            checks = iter((False, True))
            def wait_until_free() -> None:
                if events:
                    released = transport.requests[-1]
                    self.assertIsInstance(released, wire.ReleaseRequest)
                    assert isinstance(released, wire.ReleaseRequest)
                    self.assertEqual(released.reservation, GRANTED.reservation)
                    events.append("wait after release")
                else:
                    events.append("initial wait")
            def admits() -> bool:
                events.append("check hold")
                return next(checks)
            hold = client.AdmissionHoldCheck(wait_until_free, admits)
            result = client.admit(request, lambda _reply: None, lambda: request, _attempt=hold)
            self.assertEqual(result, final)
            self.assertEqual(events, ["initial wait", "check hold", "wait after release", "check hold"])
            first, release, second = transport.requests
            assert isinstance(first, wire.AdmitRequest)
            assert isinstance(release, wire.ReleaseRequest)
            assert isinstance(second, wire.AdmitRequest)
            self.assertEqual(release.reservation, GRANTED.reservation)
            self.assertNotEqual(first.episode, second.episode)
            self.assertEqual((first.owner, first.since, first.profile),
                             (second.owner, second.since, second.profile))

    def test_program_request_uses_its_checkout_instead_of_the_launcher_directory(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            checkout = str(fixture.root / "other-checkout")
            profile = records.ProgramProfile("merge-gate", "repo", checkout, records.HistoryPeakSizing(6 * GIB, records.FirstProgramPeak(6 * GIB), fixture.clock.now()))
            with mock.patch.object(step_estimator, "git_identity", side_effect=AssertionError("program inferred cargo")), \
                 mock.patch.object(client, "CLOCK", fixture.clock):
                request = client.step_request([], str(fixture.root), original.owner, original.since,
                                              "", original.episode, profile)
            self.assertEqual(request.identity.folder, checkout)
            self.assertEqual(request.profile, profile)
            self.assertEqual(request.identity.step, "merge-gate")
            self.assertEqual(request.profile.peak, records.ExpectedPeak(6 * GIB, "fallback", 0))

    def test_failed_grant_file_write_releases_the_reservation_before_refusing(self) -> None:
        from pathlib import Path
        with AdmissionFixture() as fixture:
            original = fixture.request()
            shell_state = fixture.root / "grant.sh"
            _ = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)), Exchange(reply_bytes(wire.Ok())))
            released: list[str] = []
            release = client.release
            def observe_release(reservation: str, status: int, configuration: records.MemoryConfig | wire.CleanupTiming) -> wire.Ok | wire.Refused:
                released.append(reservation)
                return release(reservation, status, configuration)
            with mock.patch.object(client, "step_request", return_value=original), \
                 mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                 mock.patch.object(client, "write_shell_grant", side_effect=OSError("grant disk failed")), \
                 mock.patch.object(client, "release", side_effect=observe_release), \
                 redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()) as output:
                status = client.main(["admit", "--pid", str(original.owner.pid),
                                      "--shell-state", str(shell_state), "--", "cargo", "check"])
            self.assertEqual(status, 2)
            self.assertEqual(released, [GRANTED.reservation])
            self.assertEqual(output.getvalue(), "")
            self.assertIn("BUILDLOG_MEM_OUTCOME=Refused", Path(shell_state).read_text())
            self.assertNotIn("BUILDLOG_MEM_OUTCOME=Granted", shell_state.read_text())

    def test_cli_refresh_uses_step_estimate_and_keeps_the_episode_and_first_since(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            updated = replace(original.profile, estimated_at=fixture.clock.now() + 1)
            transport = self.use_transport(fixture, Exchange(reply_bytes(EXPIRED)), Exchange(reply_bytes(GRANTED)))
            estimates = [step_estimator.StepEstimate(original.profile, records.FreshTest()),
                         step_estimator.StepEstimate(updated, step_estimator.ReusableEstimate())]
            with mock.patch.object(step_estimator, "step_estimate", side_effect=estimates) as estimate, \
                 mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                 redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
                status = client.main(["admit", "--pid", str(original.owner.pid), "--since", str(original.since),
                                      "--", "cargo", "nextest", "run"])
            self.assertEqual(status, 0)
            self.assertEqual(estimate.call_count, 2)
            first, second = transport.requests
            assert isinstance(first, wire.AdmitRequest)
            assert isinstance(second, wire.AdmitRequest)
            self.assertEqual((first.episode, first.since, first.owner), (second.episode, second.since, second.owner))
            self.assertEqual(first.since, original.since)
            self.assertTrue(first.fresh_test)
            self.assertFalse(second.fresh_test)
            self.assertEqual(second.profile, updated)

    def test_build_generation_precedes_history_and_survives_transport_reconnect(self) -> None:
        for expired_cache in (False, True):
            with self.subTest(expired_cache=expired_cache), AdmissionFixture() as fixture:
                owner = fixture.request()
                profile: records.InferFromArgv | wire.Profile = records.INFER_FROM_ARGV
                if expired_cache:
                    assert isinstance(owner.profile, records.StepProfile)
                    profile = replace(owner.profile, step="build", build_history_generation=records.BuildHistoryGeneration(6),
                        estimated_at=fixture.clock.now() - records.read_memory_config().policy.timing.estimate_refresh_seconds - 1)
                transport = self.use_transport(fixture, Exchange(reply_bytes(wire.BuildHistoryReply(7))),
                    Exchange(reply_bytes(WAIT), b""), Exchange(reply_bytes(GRANTED)))
                def history_peak(*_args: object, **_kwargs: object) -> records.ExpectedPeak:
                    self.assertIsInstance(transport.requests[-1], wire.BuildHistoryRequest)
                    return records.ExpectedPeak(12 * GIB, "fallback", 0)
                with mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                        mock.patch.object(step_estimator, "expected_peak", side_effect=history_peak) as history:
                    with mock.patch.object(conf, "read", wraps=conf.read) as reader:
                        request = client.step_request(["cargo", "build"], str(fixture.root), owner.owner,
                            owner.since, "", owner.episode, profile)
                    reader.assert_called_once_with("build_admission")
                    self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
                self.assertEqual(history.call_count, 1)
                self.assertEqual(sum(isinstance(item, wire.BuildHistoryRequest) for item in transport.requests), 1)
                first, second = [item for item in transport.requests if isinstance(item, wire.AdmitRequest)]
                self.assertEqual(first, second)
                assert isinstance(second.profile, records.StepProfile)
                self.assertEqual(second.profile.build_history_generation, records.BuildHistoryGeneration(7))
                self.doCleanups()

    def test_cached_build_estimate_keeps_its_original_history_generation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            with mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(client, "build_history_generation", side_effect=AssertionError("cached estimate queried a newer generation")), \
                    mock.patch.object(step_estimator, "expected_peak", side_effect=AssertionError("cached estimate read history")):
                retained = client.step_request(list(request.argv), request.cwd, request.owner,
                    request.since, request.sidecar, request.episode, request.profile)
            self.assertEqual(retained.profile, request.profile)
            self.assertEqual((retained.owner, retained.episode, retained.since),
                (request.owner, request.episode, request.since))

    def test_expired_build_reads_the_current_generation_before_refreshing_on_its_connection(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            granted = replace(GRANTED, need=16 * GIB)
            transport = self.use_transport(fixture,
                Exchange(reply_bytes(EXPIRED), reply_bytes(wire.BuildHistoryReply(1))),
                Exchange(reply_bytes(granted)))
            def history_peak(*_args: object, **_kwargs: object) -> records.ExpectedPeak:
                self.assertIsInstance(transport.requests[-1], wire.BuildHistoryRequest)
                return records.ExpectedPeak(16 * GIB, "fallback", 0)
            def refresh() -> wire.AdmitRequest:
                return client.step_request(list(request.argv), request.cwd, request.owner,
                    request.since, request.sidecar, request.episode)
            with mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                    mock.patch.object(step_estimator, "expected_peak", side_effect=history_peak) as history:
                self.assertEqual(client.admit(request, lambda _wait: None, refresh), granted)
            self.assertEqual(history.call_count, 1)
            self.assertEqual(len(transport.attempts), 2)
            first, generation, refreshed = transport.requests
            self.assertIsInstance(generation, wire.BuildHistoryRequest)
            assert isinstance(first, wire.AdmitRequest) and isinstance(refreshed, wire.AdmitRequest)
            self.assertEqual((first.owner, first.episode, first.since),
                (refreshed.owner, refreshed.episode, refreshed.since))
            assert isinstance(refreshed.profile, records.StepProfile)
            self.assertEqual(refreshed.profile.build_history_generation, records.BuildHistoryGeneration(1))
            self.assertEqual(refreshed.profile.peak.bytes, granted.need)

    def test_generation_outage_after_expiry_keeps_the_leave_hook_time_and_cli_status(self) -> None:
        for failure in (b"", b"invalid generation reply\n"):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                fixture.configure(running_pass_seconds=5, reconnect_seconds=2)
                interval = records.read_memory_config().running_pass_seconds
                original = fixture.request()
                transport = self.use_transport(fixture,
                    Exchange(reply_bytes(wire.BuildHistoryReply(0))),
                    Exchange(reply_bytes(WAIT), TimedChunk(interval - 2, reply_bytes(EXPIRED)), failure),
                    *(Exchange(connect_error=ConnectionRefusedError()) for _ in range(6)),
                    Exchange(reply_bytes(EXPIRED), reply_bytes(wire.BuildHistoryReply(1))),
                    Exchange(reply_bytes(GRANTED)))
                hook_times: list[float] = []

                def leave() -> bool:
                    hook_times.append(fixture.clock.monotonic())
                    return True

                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-leave"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=leave), \
                        mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                        mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                        mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                        redirect_stderr(io.StringIO()) as errors, mock.patch("sys.stdout", io.StringIO()) as output:
                    status = client.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "build"])
                self.assertEqual(status, 75, errors.getvalue())
                self.assertEqual(hook_times, [interval], "generation retries must keep the original hook time")
                self.assertEqual(fixture.clock.monotonic(), interval)
                self.assertEqual(output.getvalue(), "")
                self.assertNotIn("step refused", errors.getvalue())
                self.assertGreaterEqual(sum(isinstance(item, wire.BuildHistoryRequest)
                    for item in transport.requests), 2)
                admissions = [item for item in transport.requests if isinstance(item, wire.AdmitRequest)]
                self.assertTrue(admissions)
                self.assertTrue(all((item.owner, item.episode, item.since) ==
                    (admissions[0].owner, admissions[0].episode, admissions[0].since) for item in admissions))
                self.doCleanups()

    def test_unreachable_generation_refresh_stops_the_cli_without_checker_retries(self) -> None:
        for command in ("admit", "launch"):
            for generation_failure in (b"", b"invalid generation reply\n"):
                with self.subTest(command=command, failure=generation_failure), AdmissionFixture() as fixture:
                    fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
                    original = fixture.request(step="build")
                    transport = self.use_transport(fixture,
                        Exchange(reply_bytes(wire.BuildHistoryReply(0))),
                        Exchange(reply_bytes(EXPIRED), generation_failure),
                        *(Exchange(connect_error=FileNotFoundError()) for _ in range(3)))
                    with mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                            mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                            mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                            redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()):
                        status = client.main([command, "--pid", str(original.owner.pid), "--", "cargo", "build"])
                    self.assertEqual(status, 2)
                    self.assertEqual(fixture.clock.monotonic(), 5 if not generation_failure else 8)
                    self.assertEqual(sum(isinstance(request, wire.BuildHistoryRequest)
                                         for request in transport.requests), 2)
                    self.assertEqual(errors.getvalue().count("build queue not reachable; trying for 5 s"), 1)
                    self.assertEqual(errors.getvalue().count(
                        "build queue unreachable for 5 s; step stopped; cargo token given back"), 1)
                    self.assertNotIn("step refused", errors.getvalue())
                    self.doCleanups()

    def test_generation_retries_cannot_extend_the_original_launch_deadline(self) -> None:
        for stage in ("initial", "refresh"):
            for failure in ("connect", "silent"):
                with self.subTest(stage=stage, failure=failure), AdmissionFixture() as fixture:
                    fixture.configure(reconnect_seconds=2)
                    original = fixture.request()
                    exchanges: list[Exchange] = [] if stage == "initial" else [
                        Exchange(reply_bytes(wire.BuildHistoryReply(0))),
                        Exchange(TimedChunk(3, reply_bytes(EXPIRED)), b"invalid generation reply\n")]
                    exchanges.extend(Exchange(connect_error=ConnectionRefusedError()) if failure == "connect"
                        else Exchange(TimeoutError()) for _ in range(12))
                    transport = self.use_transport(fixture, *exchanges)
                    with mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "6", "LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}), \
                            mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                            mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), str(fixture.root))), \
                            mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                            redirect_stderr(io.StringIO()) as errors, mock.patch("sys.stdout", io.StringIO()) as output:
                        status = client.main(["launch", "--pid", str(original.owner.pid), "--", "cargo", "build"])
                    self.assertEqual(status, 2)
                    self.assertIn("launch wait limit reached without admission", errors.getvalue())
                    self.assertEqual(fixture.clock.monotonic(), 6, "generation retries must use the original launch deadline")
                    self.assertTrue(all(attempt < 6 for attempt in transport.attempts))
                    self.assertEqual(output.getvalue(), "")
                    self.doCleanups()

    def test_grant_sent_during_a_slow_leave_hook_is_read_before_the_next_hook(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="5")
            interval = records.read_memory_config().running_pass_seconds
            request = fixture.request()
            exchange = Exchange(reply_bytes(WAIT), TimeoutError())
            transport = self.use_transport(fixture, exchange)
            hooks: list[float] = []

            def decline_after_grant() -> bool:
                hooks.append(fixture.clock.monotonic())
                self.assertEqual(len(hooks), 1, "the client must read the grant before another hook")
                fixture.clock.advance(interval)
                exchange.events.append(reply_bytes(GRANTED))
                fixture.clock.advance(interval)
                return False

            with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "slow-decline"}), \
                    mock.patch.object(client, "_leave_queue", side_effect=decline_after_grant):
                result = client.admit(request, lambda _reply: None, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(hooks, [interval])
            self.assertEqual(transport.requests, [request])
            self.assertEqual(exchange.events, [])

    def test_split_grant_survives_a_declined_leave_hook(self) -> None:
        for kind, partial_at_deadline in (("admit", False), ("launch", False), ("admit", True), ("launch", True)):
            with self.subTest(kind=kind, partial_at_deadline=partial_at_deadline), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="5")
                interval = records.read_memory_config().running_pass_seconds
                request = fixture.request() if kind == "admit" else fixture.launch()
                grant = GRANTED if kind == "admit" else wire.LaunchAdmitted(GIB, "new-source")
                encoded = reply_bytes(grant)
                split = len(encoded) // 2
                transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT),
                    TimedChunk(interval if partial_at_deadline else interval - 1, encoded[:split]), TimeoutError(),
                    TimedChunk(1, encoded[split:])))
                hooks: list[float] = []
                waits: list[wire.Wait] = []

                def decline() -> bool:
                    hooks.append(fixture.clock.monotonic())
                    return False

                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-decline"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=decline), \
                        redirect_stderr(io.StringIO()) as errors:
                    if isinstance(request, wire.AdmitRequest):
                        admit_request = request
                        result = client.admit(admit_request, waits.append, lambda: admit_request)
                    else:
                        launch_request = request
                        result = client.launch(launch_request, waits.append, lambda: launch_request)
                self.assertEqual(result, grant)
                self.assertEqual(hooks, [interval, 2 * interval] if partial_at_deadline else [interval])
                self.assertEqual(waits, [WAIT])
                self.assertEqual(transport.requests, [request])
                self.assertEqual(len(transport.attempts), 1)
                self.assertEqual(fixture.clock.monotonic(), (2 * interval if partial_at_deadline else interval) + 1)
                self.assertEqual(errors.getvalue(), "")
                self.doCleanups()

    def test_split_reply_survives_a_silence_timeout(self) -> None:
        mutations: dict[str, Callable[[], object]] = {
            "release": lambda: client.release("stem", 137, records.read_memory_config()),
            "width": lambda: client.width("stem", 3),
            "admit-head": lambda: client.admit_head("urgent", "director"),
            "priority-set": lambda: client.priority_set("unit", "urgent", "director"),
            "priority-clear": lambda: client.priority_clear("director"),
            "priority-clear-unit": lambda: client.priority_clear("director", "demo"),
            "token-wait": lambda: client.token_wait("stem", records.read_memory_config())}
        for kind in ("admit", "launch", *mutations):
            with self.subTest(kind=kind), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", silence_seconds="7")
                interval = records.read_memory_config().silence_seconds
                request = fixture.request() if kind == "admit" else fixture.launch()
                grant = GRANTED if kind == "admit" else wire.LaunchAdmitted(GIB, "new-source")
                expected = grant if kind in ("admit", "launch") else wire.Ok()
                encoded = reply_bytes(expected)
                split = len(encoded) // 2
                transport = self.use_transport(fixture,
                    Exchange(encoded[:split], TimeoutError(), encoded[split:]))
                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}), \
                        redirect_stderr(io.StringIO()) as errors:
                    if kind == "admit":
                        assert isinstance(request, wire.AdmitRequest)
                        admit_request = request
                        result = client.admit(admit_request, lambda _wait: None, lambda: admit_request)
                    elif kind == "launch":
                        assert isinstance(request, wire.LaunchRequest)
                        launch_request = request
                        result = client.launch(launch_request, lambda _wait: None, lambda: launch_request)
                    else:
                        result = mutations[kind]()
                self.assertEqual(result, expected)
                self.assertEqual(fixture.clock.monotonic(), interval)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(len(transport.attempts), 1)
                self.assertEqual(errors.getvalue().count("admission service not answering since"), 1)
                self.assertNotIn("check failed", errors.getvalue())
                self.doCleanups()

    def test_cleanup_uses_acquired_silence_after_configuration_becomes_unusable(self) -> None:
        for kind in ("release", "token-wait"):
            with self.subTest(kind=kind), AdmissionFixture() as fixture:
                config = fixture.root / "build_admission.conf"
                write_memory_config(config, silence_seconds="7")
                acquired = records.read_memory_config()
                write_memory_config(config, silence_seconds="invalid")
                encoded = reply_bytes(wire.Ok())
                split = len(encoded) // 2
                transport = self.use_transport(fixture,
                    Exchange(encoded[:split], TimeoutError(), encoded[split:]))
                with mock.patch.object(records, "read_memory_config", side_effect=AssertionError("cleanup read config")), \
                        redirect_stderr(io.StringIO()) as errors:
                    result = client.release("stem", 137, acquired) if kind == "release" else client.token_wait("stem", acquired)
                self.assertEqual(result, wire.Ok())
                self.assertEqual(fixture.clock.monotonic(), 7)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(len(transport.attempts), 1)
                self.assertEqual(errors.getvalue().count("admission service not answering since"), 1)
                self.assertNotIn("check failed", errors.getvalue())
                self.doCleanups()

    def test_standalone_release_succeeds_after_configuration_is_corrupted(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant, = fixture.server.handle_request(request)
            assert isinstance(grant, wire.Admitted)
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, silence_seconds="invalid")
            with fixture.serving(), redirect_stderr(io.StringIO()) as errors, \
                    mock.patch("sys.stdout", io.StringIO()) as output:
                status = client.main(["release", grant.reservation, "137"])
            self.assertEqual(status, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertEqual(wire.parse_record(output.getvalue(), wire.Ok), wire.Ok())
            self.assertFalse(fixture.server.reservations)

    def test_recovered_cargo_peak_warns_once_for_the_complete_step_request(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, **{"program_first_peak_gib.cargo": "7"})
            commit_config_fixture(config)
            write_memory_config(config, **{"program_first_peak_gib.cargo": "invalid"})
            original = fixture.request()
            repository = records.RepositoryIdentity("github.com/team/project", "project")
            with mock.patch.object(step_estimator, "git_identity", return_value=(repository, "tree")), \
                    mock.patch.object(conf, "read", wraps=conf.read) as reader, \
                    redirect_stderr(io.StringIO()) as errors:
                request = client.step_request(["cargo", "clippy"], str(fixture.root), original.owner,
                                              original.since, "", original.episode)
            reader.assert_called_once_with("build_admission")
            self.assertEqual(request.profile.peak.bytes, 7 * GIB)
            self.assertEqual(request.profile.peak.source, "fallback")
            self.assertEqual(errors.getvalue().count("program_first_peak_gib.cargo='invalid'"), 1)
            self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_recovered_key_with_an_unavailable_key_prints_only_the_refusal(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="321", **{"program_first_peak_gib.cargo": None})
            commit_config_fixture(config)
            write_memory_config(config, estimate_refresh_seconds="invalid", **{"program_first_peak_gib.cargo": "invalid"})
            original = fixture.request()
            with redirect_stderr(io.StringIO()) as errors:
                status = client.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "clippy"])
            self.assertEqual(status, 2)
            self.assertIn(str(config), errors.getvalue())
            self.assertIn("program_first_peak_gib.cargo='invalid'", errors.getvalue())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)
            self.assertNotIn("warning:", errors.getvalue())
            self.assertFalse(fixture.server.reservations)

    def test_split_generation_reply_survives_a_declined_leave_hook(self) -> None:
        for stage in ("initial", "refresh"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="0.25")
                interval = records.read_memory_config().running_pass_seconds
                original = fixture.request(step="build")
                encoded = reply_bytes(wire.BuildHistoryReply(7))
                split = len(encoded) // 2
                prefix = [] if stage == "initial" else [reply_bytes(EXPIRED)]
                transport = self.use_transport(fixture,
                    Exchange(*prefix, TimedChunk(interval / 2, encoded[:split]), TimeoutError(),
                        TimedChunk(interval / 4, encoded[split:])),
                    Exchange(reply_bytes(GRANTED)))
                hooks: list[float] = []

                def decline() -> bool:
                    hooks.append(fixture.clock.monotonic())
                    return False

                def refresh() -> wire.AdmitRequest:
                    return client.step_request(list(original.argv), original.cwd, original.owner,
                        original.since, original.sidecar, original.episode)

                waits: list[wire.Wait] = []
                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-decline"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=decline), \
                        mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), original.cwd)), \
                        mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)) as history, \
                        redirect_stderr(io.StringIO()) as errors:
                    request = refresh() if stage == "initial" else original
                    result = client.admit(request, waits.append, refresh)
                self.assertEqual(result, GRANTED)
                self.assertEqual(hooks, [interval])
                self.assertEqual(waits, [] if stage == "initial" else [EXPIRED])
                self.assertEqual(history.call_count, 1)
                self.assertEqual(len(transport.attempts), 2)
                self.assertEqual(sum(isinstance(item, wire.BuildHistoryRequest) for item in transport.requests), 1)
                admissions = [item for item in transport.requests if isinstance(item, wire.AdmitRequest)]
                self.assertTrue(all((item.owner, item.episode, item.since) ==
                    (original.owner, original.episode, original.since) for item in admissions))
                self.assertEqual(len(admissions), 1 if stage == "initial" else 2)
                assert isinstance(admissions[-1].profile, records.StepProfile)
                self.assertEqual(admissions[-1].profile.build_history_generation, records.BuildHistoryGeneration(7))
                self.assertEqual(errors.getvalue(), "")
                self.doCleanups()

    def test_complete_reply_retains_the_partial_next_reply(self) -> None:
        for next_reply in ("grant", "generation"):
            with self.subTest(next_reply=next_reply), AdmissionFixture() as fixture:
                write_memory_config(fixture.root / "build_admission.conf", running_pass_seconds="0.25")
                interval = records.read_memory_config().running_pass_seconds
                request = fixture.request(step="build")
                first = WAIT if next_reply == "grant" else EXPIRED
                encoded = reply_bytes(GRANTED if next_reply == "grant" else wire.BuildHistoryReply(7))
                split = len(encoded) // 2
                exchange = Exchange(reply_bytes(first) + encoded[:split], TimeoutError(), encoded[split:])
                exchanges = [exchange] if next_reply == "grant" else [exchange, Exchange(reply_bytes(GRANTED))]
                transport = self.use_transport(fixture, *exchanges)
                hooks: list[float] = []
                waits: list[wire.Wait] = []

                def decline() -> bool:
                    hooks.append(fixture.clock.monotonic())
                    return False

                def refresh() -> wire.AdmitRequest:
                    return client.step_request(list(request.argv), request.cwd, request.owner,
                        request.since, request.sidecar, request.episode)

                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-decline"}), \
                        mock.patch.object(client, "_leave_queue", side_effect=decline), \
                        mock.patch.object(step_estimator, "git_identity", return_value=(records.UnknownRepository(), request.cwd)), \
                        mock.patch.object(step_estimator, "expected_peak", return_value=records.ExpectedPeak(12 * GIB, "fallback", 0)), \
                        redirect_stderr(io.StringIO()) as errors:
                    result = client.admit(request, waits.append, refresh)
                self.assertEqual(result, GRANTED)
                self.assertEqual(waits, [first])
                self.assertEqual(hooks, [interval])
                self.assertEqual(len(transport.attempts), 1 if next_reply == "grant" else 2)
                self.assertEqual(sum(isinstance(item, wire.BuildHistoryRequest) for item in transport.requests),
                    0 if next_reply == "grant" else 1)
                self.assertEqual(errors.getvalue(), "")
                self.doCleanups()

    def test_reconnect_discards_a_partial_reply_from_the_old_socket(self) -> None:
        for kind in ("admit", "generation"):
            with self.subTest(kind=kind), AdmissionFixture() as fixture:
                request = fixture.request()
                expected = GRANTED if kind == "admit" else wire.BuildHistoryReply(7)
                encoded = reply_bytes(expected)
                old = Exchange(encoded[:len(encoded) // 2], TimeoutError(), b"")
                transport = self.use_transport(fixture, old, Exchange(encoded))
                with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "", "BUILDLOG_MAC_IDLE_HOOK": ""}), \
                        redirect_stderr(io.StringIO()) as errors:
                    result = client.admit(request, lambda _wait: None, lambda: request) if kind == "admit" \
                        else client.build_history_generation()
                self.assertEqual(result, GRANTED if kind == "admit" else records.BuildHistoryGeneration(7))
                self.assertEqual(len(transport.attempts), 2)
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(transport.requests[0], transport.requests[1])
                self.assertNotIn("check failed", errors.getvalue())
                self.doCleanups()

    def test_down_alert_sends_once_until_a_restart_runs_for_the_silence_interval(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()
            fixture.configure(silence_seconds=10, reconnect_seconds=1)
            commands: list[tuple[list[str], str]] = []

            def deliver(command: list[str], *, input: str, text: bool, capture_output: bool,
                        timeout: float) -> subprocess.CompletedProcess[str]:
                self.assertTrue(text)
                self.assertTrue(capture_output)
                self.assertGreater(timeout, 0)
                commands.append((command, input))
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(client.down_alert(), 0)
                self.assertRegex(output.getvalue(), r"^already claimed by [0-9a-f]+\n$")
                self.assertEqual(len(commands), sender.call_count)
                return subprocess.CompletedProcess(command, 0, "SENT: to builds\n", "")

            with mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(client, "view", side_effect=wire.AdmissionUnavailable("no socket")), \
                    mock.patch.object(subprocess, "run", side_effect=deliver) as sender:
                with redirect_stdout(io.StringIO()) as sent_output:
                    self.assertEqual(client.down_alert(), 0)
                self.assertEqual(sent_output.getvalue(), "SENT: to builds\n")
                for attempt in range(3):
                    with self.subTest(restart=attempt), fixture.loop_turn():
                        fixture.restart()
                        fixture.clock.advance(9)
                        fixture.server.run_once()
                        self.assertIsInstance(outage_alerts.claim("build-admission"), outage_alerts.AlertDelivered)
                self.assertEqual(client.down_alert(), 0)
                self.assertEqual(sender.call_count, 1)
                with fixture.loop_turn():
                    fixture.server.run_once()
                self.assertEqual(client.down_alert(), 0)
                self.assertEqual(sender.call_count, 2)
            command, message = commands[0]
            self.assertEqual(command[command.index("--to") + 1], "builds")
            self.assertEqual(command[command.index("--from") + 1], "build-admission")
            self.assertEqual(message,
                "The build queue service has not answered for 10 seconds. Builds wait.\n"
                + "The service restarts by itself. Read journalctl -u build-admission.service.\n"
                + "If the user must act, push the phone with this command: "
                + "sudo systemctl restart build-admission.socket build-admission.service")
            self.assertEqual(commands[1], commands[0])

    def test_down_alert_retains_sent_or_waiting_messages_and_retries_only_empty_deliveries(self) -> None:
        cases: tuple[tuple[send_result.Outcome, send_result.Outcome, bool], ...] = (
            ("queued", "sent", True), ("held", "failed", True), ("pending", "failed", True),
            ("queued", "failed", True), ("failed", "sent", True), ("failed", "queued", True),
            ("failed", "held", True), ("failed", "pending", True), ("failed", "skipped", True),
            ("skipped", "failed", True), ("failed", "failed", False),
            ("refused", "refused", False), ("retry", "retry", False))
        for session, phone, retained in cases:
            with self.subTest(session=session, phone=phone), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=2, reconnect_seconds=1)
                replies = [subprocess.CompletedProcess([], send_result.EXIT[outcome],
                    f"{outcome.upper()}: stub delivery\n", "") for outcome in (session, phone)]
                with mock.patch.object(client, "CLOCK", fixture.clock), \
                        mock.patch.object(client, "view", side_effect=wire.AdmissionUnavailable("no socket")), \
                        mock.patch.object(subprocess, "run", side_effect=replies * 2) as sender:
                    self.assertEqual(client.down_alert(), 0 if retained else 1)
                    self.assertEqual(sender.call_count, 2)
                    command = cast(list[str], sender.call_args_list[1].args[0])
                    self.assertEqual(command[command.index("--to") + 1], "user")
                    self.assertEqual(command[command.index("--text-id") + 1], "alert.blocked")
                    self.assertEqual(command[command.index("--need") + 1], "blocked")
                    values = [command[index + 1] for index, part in enumerate(command) if part == "--value"]
                    self.assertEqual(values, [
                        "action=On natedev, run: sudo systemctl restart build-admission.socket build-admission.service",
                        "body=The builds session did not take the message. Builds wait until the service answers.",
                        "sender=build-admission", "title=Build queue service is down"])
                    scripts = Path(client.__file__).resolve().parents[1]
                    self.assertEqual(shlex.split(command[command.index("--clears-when") + 1]),
                        [str(scripts / "lib/py"), str(Path(client.__file__).resolve()), "view"])
                    self.assertEqual(client.down_alert(), 0 if retained else 1)
                    self.assertEqual(sender.call_count, 2 if retained else 4)

    def test_outage_clear_command_reports_failure_while_the_private_service_is_down(self) -> None:
        with AdmissionFixture() as fixture, redirect_stderr(io.StringIO()) as errors:
            fixture.server.close()
            self.assertNotEqual(client.main(["view"]), 0)
            self.assertIn("admission service unavailable:", errors.getvalue())

    def test_retry_and_outage_notice_intervals_read_config_during_the_call(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reconnect_seconds=3, outage_notice_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture,
                *(Exchange(connect_error=FileNotFoundError()) for _ in range(3)),
                Exchange(reply_bytes(GRANTED)))
            def reconfigure(_seconds: float) -> None:
                if fixture.clock.monotonic() == 3:
                    fixture.configure(reconnect_seconds=4, outage_notice_seconds=6)
            fixture.clock.on_sleep = reconfigure
            with redirect_stderr(io.StringIO()) as output, \
                    mock.patch.object(records, "read_memory_config", wraps=records.read_memory_config) as reader:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            reader.assert_called_once_with(report=False)
            self.assertEqual(transport.attempts, [0, 3, 6, 9])
            self.assertEqual(output.getvalue().count("admission service unavailable:"), 2)

    def test_unreachable_limit_stops_missing_and_refused_connections(self) -> None:
        for error in (FileNotFoundError(), ConnectionRefusedError()):
            with self.subTest(error=type(error).__name__), AdmissionFixture() as fixture:
                fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
                request = fixture.request()
                transport = self.use_transport(fixture, *(Exchange(connect_error=error) for _ in range(3)))
                with redirect_stderr(io.StringIO()) as output:
                    result = client.admit(request, lambda _wait: None, lambda: request)
                self.assertIsInstance(result, wire.Refused)
                self.assertEqual(fixture.clock.monotonic(), 5)
                self.assertEqual(transport.attempts, [0, 3])
                self.assertEqual(output.getvalue().count("build queue not reachable; trying for 5 s"), 1)
                self.assertEqual(output.getvalue().count(
                    "build queue unreachable for 5 s; step stopped; cargo token given back"), 1)
                self.doCleanups()

    def test_repeated_accepted_connections_without_replies_stop_at_the_unreachable_limit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=0.3)
            request = fixture.request()
            transport = self.use_transport(fixture,
                *(Exchange(TimedChunk(0.125, b"")) for _ in range(8)))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, wire.Refused(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"))
            self.assertGreaterEqual(fixture.clock.monotonic(), 0.3)
            self.assertGreater(len(transport.requests), 1)
            self.assertEqual(transport.requests, [request] * len(transport.requests))
            self.assertEqual(output.getvalue().count(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"), 1)

    def test_valid_wait_after_restart_keeps_the_place_beyond_the_first_unreachable_deadline(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=0.3)
            request = fixture.request()
            waits: list[wire.Wait] = []
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")),
                Exchange(TimedChunk(0.15, reply_bytes(WAIT)), TimedChunk(0.25, b"")),
                Exchange(reply_bytes(WAIT) + reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                started = fixture.clock.monotonic()
                result = client.admit(request, waits.append, lambda: request)
                elapsed = fixture.clock.monotonic() - started
            self.assertEqual(result, GRANTED)
            self.assertGreater(elapsed, 0.35)
            self.assertEqual(transport.requests, [request] * 3)
            self.assertEqual(waits, [WAIT, WAIT])
            self.assertNotIn("step stopped", output.getvalue())

    def test_reset_then_accepted_silent_connection_stops_at_the_unreachable_limit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=0.3, silence_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")), Exchange(TimeoutError()))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, wire.Refused(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"))
            self.assertGreaterEqual(fixture.clock.monotonic(), 0.3)
            self.assertEqual(transport.requests, [request, request])
            self.assertEqual(output.getvalue().count(
                "build queue unreachable for 0.3 s; step stopped; cargo token given back"), 1)

    def test_a_valid_reply_after_the_unreachable_limit_keeps_the_wait(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(unreachable_limit_seconds=5)
            request = fixture.request()
            waits: list[wire.Wait] = []
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")),
                Exchange(TimedChunk(6, reply_bytes(WAIT)), reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(waits, [WAIT])
            self.assertEqual(transport.requests, [request, request])
            self.assertNotIn("step stopped", output.getvalue())

    def test_an_owner_that_left_past_the_unreachable_limit_gets_left_queue(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(running_pass_seconds=3, unreachable_limit_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture,
                Exchange(ConnectionResetError(errno.ECONNRESET, "reset")),
                Exchange(TimedChunk(6, b"incomplete reply")))
            with mock.patch.dict(os.environ, {"LINT_QUEUE_LEAVE_HOOK": "test-leave"}), \
                    mock.patch.object(client, "_leave_queue", return_value=True) as leave, \
                    redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, client.LeftQueue())
            leave.assert_called_once_with()
            self.assertEqual(transport.requests, [request, request])
            self.assertNotIn("step stopped", output.getvalue())

    def test_a_valid_reply_resets_the_unreachable_limit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reconnect_seconds=3, unreachable_limit_seconds=5)
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(connect_error=FileNotFoundError()),
                Exchange(reply_bytes(WAIT), ConnectionResetError(errno.ECONNRESET, "reset")), Exchange(connect_error=FileNotFoundError()),
                Exchange(reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            self.assertEqual(transport.attempts, [0, 3, 3, 6])
            self.assertEqual(output.getvalue().count("build queue not reachable; trying for 5 s"), 2)
            self.assertNotIn("step stopped", output.getvalue())

    def test_silence_notice_uses_the_configured_interval(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(silence_seconds=7)
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(TimeoutError(), reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            self.assertEqual(fixture.clock.monotonic(), 7)
            self.assertEqual(transport.timeouts, [7, 7, 7])
            self.assertEqual(output.getvalue().count("admission service not answering since"), 1)

    def test_edited_transport_timing_applies_to_the_next_call(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            fixture.configure(reconnect_seconds=3, outage_notice_seconds=5)
            transport = self.use_transport(fixture,
                Exchange(connect_error=FileNotFoundError()), Exchange(reply_bytes(GRANTED)),
                Exchange(connect_error=FileNotFoundError()), Exchange(reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()), \
                    mock.patch.object(records, "read_memory_config", wraps=records.read_memory_config) as reader:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
                fixture.configure(reconnect_seconds=7, outage_notice_seconds=11)
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            self.assertEqual(reader.call_args_list, [mock.call(report=False), mock.call(report=False)])
            self.assertEqual(transport.attempts, [0, 3, 3, 10])

    def test_recovered_service_setting_warns_once_at_the_client_boundary(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="321")
            commit_config_fixture(config)
            write_memory_config(config, estimate_refresh_seconds="invalid")
            request = fixture.request()
            with fixture.serving(), mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                    mock.patch.object(client, "CLOCK", fixture.clock), redirect_stderr(io.StringIO()) as errors:
                first = client.admit(request, lambda _wait: None, lambda: request)
                second = client.admit(request, lambda _wait: None, lambda: request)
            self.assertIsInstance(first, wire.Admitted)
            self.assertEqual(second, first)
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            self.assertEqual(policy.timing.estimate_refresh_seconds, 321)
            self.assertIn(str(config), errors.getvalue())
            self.assertIn("estimate_refresh_seconds='invalid'", errors.getvalue())
            self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_wait_callback_then_admission_accepts_new_source_names(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT), reply_bytes(GRANTED)))
            waits: list[wire.Wait] = []
            result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(waits, [WAIT])
            self.assertEqual(transport.requests, [request])

    def test_explicit_refusal_is_terminal_without_retry_or_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            refusal = wire.Refused("client version 2, service supports 1..1")
            transport = self.use_transport(fixture, Exchange(reply_bytes(refusal)))
            refresh = mock.Mock(side_effect=AssertionError("must not refresh refusal"))
            self.assertEqual(client.admit(request, lambda _wait: None, refresh), refusal)
            self.assertEqual(len(transport.attempts), 1)
            refresh.assert_not_called()
            self.assertFalse(fixture.clock.sleeps)

    def test_no_socket_and_refused_connect_retry_every_two_seconds_from_first_call(self) -> None:
        for error in (FileNotFoundError("missing"), ConnectionRefusedError("refused")):
            with self.subTest(error=type(error).__name__), AdmissionFixture() as fixture:
                fixture.configure(unreachable_limit_seconds=120)
                request = fixture.request()
                failures = [Exchange(connect_error=error) for _ in range(31)]
                transport = self.use_transport(fixture, *failures, Exchange(reply_bytes(wire.Refused("test stop"))))
                with redirect_stderr(io.StringIO()) as output:
                    result = client.admit(request, lambda _wait: None, lambda: request)
                self.assertEqual(result, wire.Refused("test stop"))
                self.assertEqual(transport.attempts, [2.0 * index for index in range(32)])
                self.assertEqual(output.getvalue().count("admission service unavailable:"), 2)
                self.assertEqual(transport.requests, [request])
                self.doCleanups()

    def test_closed_connection_reconnects_immediately_with_same_episode_and_since(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(WAIT), b""),
                Exchange(reply_bytes(WAIT), b""), Exchange(reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(transport.requests, [request] * 3)
            self.assertEqual(transport.attempts, [0, 0, 0])
            self.assertEqual(output.getvalue().count("admission service restarted; waiting again"), 1)

    def test_silent_service_reports_after_120_seconds_and_keeps_waiting(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            _ = self.use_transport(fixture, Exchange(TimeoutError(), reply_bytes(GRANTED)))
            with redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertEqual(fixture.clock.monotonic(), 120)
            self.assertIn("admission service not answering since 12:00", output.getvalue())

    def test_queries_are_bounded_without_retry_and_cli_reports_reason(self) -> None:
        queries: tuple[tuple[str, Callable[[], object]], ...] = (
            ("view", client.view), ("explain", client.explain), ("mine", lambda: client.mine("session")))
        for name, query in queries:
            for event, reason in ((Exchange(connect_error=FileNotFoundError()), "no socket"),
                                  (Exchange(connect_error=ConnectionRefusedError()), "connect refused"),
                                  (Exchange(TimeoutError()), "no answer in 0.5 s")):
                with self.subTest(query=name, reason=reason), AdmissionFixture() as fixture:
                    transport = self.use_transport(fixture, event)
                    with self.assertRaises(wire.AdmissionUnavailable) as error:
                        _ = query()
                    self.assertEqual(error.exception.reason, reason)
                    self.assertLessEqual(fixture.clock.monotonic(), 0.5)
                    self.assertEqual(len(transport.attempts), 1)
                    self.assertFalse(fixture.clock.sleeps)
                    self.doCleanups()
            with self.subTest(cli=name), AdmissionFixture() as fixture:
                _ = self.use_transport(fixture, Exchange(connect_error=FileNotFoundError()))
                argv = ["admission_client", name] + (["session"] if name == "mine" else [])
                with mock.patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(client.main(), 2)
                self.assertIn("admission service unavailable: no socket", output.getvalue())
                self.doCleanups()

    def test_query_timeout_uses_configuration_for_the_complete_reply(self) -> None:
        with AdmissionFixture() as fixture:
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            encoded = reply_bytes(reply)
            connect, send = ScriptSocket.connect, ScriptSocket.sendall
            for timeout in (0.8, 1.4):
                fixture.configure(query_timeout_seconds=timeout)
                for succeeds in (True, False):
                    with self.subTest(timeout=timeout, succeeds=succeeds):
                        start = fixture.clock.monotonic()
                        last_chunk = TimedChunk(timeout / 4, encoded[-1:]) if succeeds else TimeoutError()
                        transport = self.use_transport(fixture, Exchange(
                            TimedChunk(timeout / 4, encoded[:-1]), last_chunk))

                        def connect_slowly(connection: ScriptSocket, path: str) -> None:
                            connect(connection, path)
                            fixture.clock.advance(timeout / 8)

                        def send_slowly(connection: ScriptSocket, data: bytes) -> None:
                            send(connection, data)
                            fixture.clock.advance(timeout / 8)

                        with mock.patch.object(ScriptSocket, "connect", connect_slowly), \
                             mock.patch.object(ScriptSocket, "sendall", send_slowly):
                            if succeeds:
                                self.assertEqual(client.view(), reply.reading)
                            else:
                                with self.assertRaises(wire.AdmissionUnavailable) as error:
                                    _ = client.view()
                                self.assertEqual(error.exception.reason, f"no answer in {timeout:g} s")
                        self.assertAlmostEqual(fixture.clock.monotonic() - start, timeout * (0.75 if succeeds else 1))
                        self.assertAlmostEqual(transport.timeouts[1], timeout * 7 / 8)
                        self.assertEqual(len(transport.attempts), 1)
                        self.assertFalse(fixture.clock.sleeps)
                        self.doCleanups()

    def test_successful_queries_return_the_domain_record(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request())
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            reading = reply.reading
            _ = self.use_transport(fixture, Exchange(reply_bytes(reply)),
                Exchange(reply_bytes(wire.ExplainReply(reading.explanation))),
                Exchange(reply_bytes(wire.MineReply(reading))))
            self.assertEqual(client.view(), reading)
            self.assertEqual(client.explain(), reading.explanation)
            self.assertEqual(client.mine("session"), reading)

    def test_estimate_refresh_changes_only_profile_and_freshness(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(since=fixture.clock.now() - 700)
            updated = replace(request, episode="must-not-replace", since=fixture.clock.now(),
                owner=wire.Owner(1, "wrong"), profile=replace(request.profile,
                    peak=records.ExpectedPeak(2 * GIB, "measured", 9)), fresh_test=True)
            transport = self.use_transport(fixture, Exchange(reply_bytes(EXPIRED)), Exchange(reply_bytes(GRANTED)))
            refresh = mock.Mock(return_value=updated)
            self.assertEqual(client.admit(request, lambda _wait: None, refresh), GRANTED)
            self.assertEqual(transport.requests[1], replace(request, profile=updated.profile, fresh_test=True))
            self.assertEqual(refresh.call_count, 1)

    def test_bad_reply_before_first_wait_refuses_without_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            transport = self.use_transport(fixture, Exchange(b"not JSON\n"))
            result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertIsInstance(result, wire.Refused)
            self.assertEqual(len(transport.attempts), 1)
            self.assertFalse(fixture.clock.sleeps)

    def test_refresh_failures_after_wait_keep_place_until_retry_limit(self) -> None:
        for setting, expected in (("6", 6), ("6.5", 60)):
            with self.subTest(setting=setting), AdmissionFixture() as fixture:
                request = fixture.request()
                transport = self.use_transport(fixture, Exchange(reply_bytes(EXPIRED)))
                refresh = mock.Mock(side_effect=ValueError(""))
                with mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": setting}), \
                     redirect_stderr(io.StringIO()) as output:
                    result = client.admit(request, lambda _wait: None, refresh)
                self.assertEqual(result, wire.Refused("checker returned an error"))
                self.assertEqual(fixture.clock.monotonic(), expected)
                self.assertEqual(transport.requests, [request])
                self.assertIn("memory admission check failed (checker returned an error); trying again, the step keeps its place",
                              output.getvalue())
                self.doCleanups()

    def test_malformed_replies_retry_same_episode_and_valid_wait_resets_failure_clock(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            bad = b"not JSON\n"
            transport = self.use_transport(fixture,
                Exchange(reply_bytes(WAIT), bad),
                Exchange(TimedChunk(2, bad)),
                Exchange(reply_bytes(WAIT), TimedChunk(4, bad)),
                Exchange(reply_bytes(GRANTED)))
            with mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": "6"}), \
                 redirect_stderr(io.StringIO()) as output:
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertEqual(result, GRANTED)
            self.assertGreaterEqual(fixture.clock.monotonic(), 6)
            self.assertTrue(all(value == request for value in transport.requests))
            self.assertIn("trying again, the step keeps its place", output.getvalue())

    def test_malformed_replies_exhaust_checker_retry_bound(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            _ = self.use_transport(fixture, Exchange(reply_bytes(WAIT), b"bad\n"),
                *(Exchange(TimedChunk(2, b"bad\n")) for _ in range(3)))
            with mock.patch.dict(os.environ, {"BUILDLOG_MEM_CHECK_RETRY_S": "6"}), redirect_stderr(io.StringIO()):
                result = client.admit(request, lambda _wait: None, lambda: request)
            self.assertIsInstance(result, wire.Refused)
            self.assertGreaterEqual(fixture.clock.monotonic(), 6)

    def test_launch_deadline_includes_missing_socket_silence_and_checker_retries(self) -> None:
        for mode in ("missing", "silent", "checker", "hold"):
            with self.subTest(mode=mode), AdmissionFixture() as fixture:
                request = fixture.launch()
                exchanges = ([Exchange(connect_error=FileNotFoundError()) for _ in range(3)] if mode == "missing" else
                    [Exchange(TimeoutError())] if mode == "silent" else
                    [Exchange(reply_bytes(EXPIRED))] if mode == "checker" else
                    [Exchange(TimedChunk(6, reply_bytes(WAIT)))])
                _ = self.use_transport(fixture, *exchanges)
                refresh = mock.Mock(side_effect=ValueError("broken")) if mode == "checker" else lambda: request
                with mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "6"}), redirect_stderr(io.StringIO()):
                    result = client.launch(request, lambda _wait: None, refresh)
                self.assertEqual(result, wire.Refused("launch wait limit reached without admission"))
                self.assertEqual(fixture.clock.monotonic(), 6)
                self.doCleanups()

    def test_launch_zero_limit_refuses_first_hold_but_accepts_immediate_grant(self) -> None:
        for reply in (WAIT, wire.LaunchAdmitted(GIB, "new-source")):
            with self.subTest(reply=reply), AdmissionFixture() as fixture:
                request = fixture.launch()
                _ = self.use_transport(fixture, Exchange(TimedChunk(0.01, reply_bytes(reply))))
                with mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "0"}):
                    result = client.launch(request, lambda _wait: None, lambda: request)
                self.assertEqual(result, wire.Refused("launch wait limit reached without admission")
                                 if isinstance(reply, wire.Wait) else reply)
                self.doCleanups()

    def test_mutations_resend_the_same_request_id_after_transport_closes(self) -> None:
        mutations: tuple[Callable[[], object], ...] = (
            lambda: client.release("stem", 137, records.read_memory_config()), lambda: client.width("stem", 3),
            lambda: client.admit_head("urgent", "director"))
        for mutation in mutations:
            with self.subTest(mutation=mutation), AdmissionFixture() as fixture:
                transport = self.use_transport(fixture, Exchange(b""), Exchange(reply_bytes(wire.Ok())))
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(mutation(), wire.Ok())
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(transport.requests[0], transport.requests[1])
                self.doCleanups()

    def test_cli_sends_one_run_identity_through_estimate_refresh_and_reconnect(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            original = fixture.request()
            transport = self.use_transport(fixture,
                Exchange(reply_bytes(WAIT), reply_bytes(EXPIRED)),
                Exchange(reply_bytes(GRANTED)))
            calls: list[int] = []

            def estimate(*_args: object, **_kwargs: object) -> step_estimator.StepEstimate:
                calls.append(len(calls))
                if len(calls) == 2:
                    os.environ["BUILDLOG_RUN"] = "999999:111111"
                return step_estimator.StepEstimate(original.profile, step_estimator.ReusableEstimate())

            with mock.patch.dict(os.environ, {"BUILDLOG_RUN": f"{run.pid}:{run.start_time}"}), \
                 mock.patch.object(step_estimator, "step_estimate", side_effect=estimate), \
                 mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                 redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()) as output:
                status = client.main(["admit", "--pid", str(original.owner.pid),
                                      "--since", str(original.since), "--wait-events", "--", "cargo", "nextest"])
            self.assertEqual(status, 0)
            self.assertEqual(len(calls), 2)
            first, second = transport.requests
            assert isinstance(first, wire.AdmitRequest)
            assert isinstance(second, wire.AdmitRequest)
            self.assertEqual((first.run, second.run), (run, run))
            self.assertEqual((first.owner, first.episode, first.since),
                             (second.owner, second.episode, second.since))
            self.assertEqual(output.getvalue().splitlines(), ["first", 'holder {"kind":"none"}', "done"])

    def test_missing_or_malformed_run_keeps_the_step_identity_and_admits(self) -> None:
        for value in (None, "", "missing", "42", "0:123", "-4:123", "abc:123", "42:", "42:abc", "42:1:2"):
            with self.subTest(value=value), AdmissionFixture() as fixture:
                original = fixture.request()
                transport = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)))
                environment = dict(os.environ)
                if value is None:
                    _ = environment.pop("BUILDLOG_RUN", None)
                else:
                    environment["BUILDLOG_RUN"] = value
                with mock.patch.dict(os.environ, environment, clear=True), \
                     mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                         original.profile, step_estimator.ReusableEstimate())), \
                     mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                     redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
                    status = client.main(["admit", "--pid", str(original.owner.pid),
                                          "--since", str(original.since), "--", "cargo", "nextest"])
                self.assertEqual(status, 0)
                request, = transport.requests
                assert isinstance(request, wire.AdmitRequest)
                self.assertIsInstance(request.run, wire.NoRun)
                self.assertEqual((request.owner, request.since), (original.owner, original.since))
            self.doCleanups()

    def test_mac_client_sends_no_run_even_with_an_inherited_linux_identity(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            original = fixture.request()
            transport = self.use_transport(fixture, Exchange(reply_bytes(GRANTED)))
            with mock.patch.object(sys, "platform", "darwin"), \
                 mock.patch.dict(os.environ, {"BUILDLOG_RUN": f"{run.pid}:{run.start_time}"}), \
                 mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                     original.profile, step_estimator.ReusableEstimate())), \
                 mock.patch.object(records, "process_start_time", return_value=original.owner.start_time), \
                 redirect_stderr(io.StringIO()), mock.patch("sys.stdout", io.StringIO()):
                status = client.main(["admit", "--pid", str(original.owner.pid), "--", "cargo", "nextest"])
            self.assertEqual(status, 0)
            request, = transport.requests
            assert isinstance(request, wire.AdmitRequest)
            self.assertIsInstance(request.run, wire.NoRun)
