"""The wire transports admission's domain records without another representation."""
from __future__ import annotations

from scripts.build_queue import snapshot_codec as codec

from scripts.lint.memory_test_support import admission_policy_fixture

from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import unittest
from typing import Literal, cast
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.lint import memory_admit as gate
from scripts.build_queue import admission_client as client, admission_protocol as wire, queue_evidence
from scripts.build_queue import admission_code, admission_records as records
from scripts.build_queue.admission_test_support import admitted_reply, wait_reply
from scripts.build_queue.admission_test_support import AdmissionFixture, Exchange, ScriptedTransport, GIB, NOW


class ProtocolTests(unittest.TestCase):

    def test_service_estimate_request_carries_identity_without_a_client_estimate(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.service_request()
            encoded = wire.encode_request(request)
            decoded = wire.parse_request(encoded)
            self.assertEqual(decoded, request)
            envelope = cast(dict[str, object], json.loads(encoded))
            fields = cast(dict[str, object], envelope["data"])
            self.assertNotIn("profile", fields)
            self.assertNotIn("history", fields)
            self.assertEqual(wire.parse_reply(wire.encode_reply(admitted_reply()), request), admitted_reply())
            grant = admitted_reply()
            runner = wire.RunnerAdmitted(grant.reservation, grant.need, grant.source,
                grant.max_path, grant.wave_path, grant.sampling_context, grant.cleanup_timing,
                execution=wire.RunnerExecutionTiming(0.1, 0.2, 1.0))
            self.assertEqual(wire.parse_reply(wire.encode_reply(runner), request), runner)
            with self.assertRaises(ValueError):
                _ = wire.parse_reply(wire.encode_reply(wire.LaunchAdmitted(GIB, "wrong outcome")), request)


    def test_wait_holder_identity_survives_the_wire_for_each_holder_kind(self) -> None:
        for holder in (records.SessionHolder("session-id", "worker"), records.UnitHolder("compiler"), records.NoHolder()):
            with self.subTest(holder=holder):
                reply = wait_reply(records.NeedsMemory())
                reply = replace(reply, snapshot=replace(reply.snapshot, holder=holder))
                parsed = wire.parse_reply(wire.encode_reply(reply))
                self.assertEqual(parsed, reply)
                assert isinstance(parsed, wire.Wait)
                self.assertEqual(parsed.snapshot.holder, holder)

    def test_an_unknown_snapshot_field_keeps_the_reply_and_the_client_wait(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            reply = wait_reply(records.NeedsMemory())
            encoded = cast(dict[str, object], json.loads(wire.encode_reply(reply)))
            data = cast(dict[str, object], encoded["data"])
            snapshot = cast(dict[str, object], data["snapshot"])
            snapshot["future_wait_evidence"] = {"kind": "UnknownFutureEvidence", "value": 17}
            line = json.dumps(encoded) + "\n"
            self.assertEqual(wire.parse_reply(line), reply)
            granted = admitted_reply()
            transport = ScriptedTransport(fixture.clock, Exchange((line + wire.encode_reply(granted)).encode()))
            waits: list[wire.Wait] = []
            with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch.object(
                    socket, "socket", side_effect=transport.socket), redirect_stderr(io.StringIO()):
                result = client.admit(request, waits.append, lambda: request)
            self.assertEqual(waits, [reply])
            self.assertEqual(result, granted)
            self.assertEqual(len(transport.requests), 1)

    def test_room_cap_and_unavailable_pressure_survive_the_service_reply(self) -> None:
        from scripts.build_queue.queue_fixture import QueueFixture
        with QueueFixture() as fixture:
            inputs = fixture.inputs(policy=admission_policy_fixture())
            running = replace(inputs.running[0], promise=6 * GIB,
                measured=gate.MeasuredMemory(2 * GIB, swap_bytes=4 * GIB,
                                            anonymous=gate.AnonymousRunUse(2 * GIB)))
            reservation = inputs.reservations[0].copy()
            reservation["need"] = 6 * GIB
            inputs = replace(inputs, running=(running,), reservations=(reservation,),
                host=gate.HostMemory(10 * GIB, 60 * GIB),
                pressure=gate.PressureUnavailable("PSI unreadable"))
            reading = gate.queue_read_from(inputs, admission_policy_fixture())
            reply = wire.ViewReply(reading)
            parsed = wire.parse_reply(wire.encode_reply(reply))
            self.assertEqual(parsed, reply)
            assert isinstance(parsed, wire.ViewReply)
            room = parsed.reading.explanation.room
            assert isinstance(room, gate.MemoryRoom)
            self.assertEqual((room.room, room.kept, room.used, room.cap),
                             (3 * GIB, 4 * GIB, 2 * GIB, 9 * GIB))
            self.assertEqual(parsed.reading.view.running[0].measured, running.measured)
            self.assertIsInstance(parsed.reading.explanation.host, gate.HostAvailable)
            assert isinstance(parsed.reading.explanation.host, gate.HostAvailable)
            self.assertEqual(parsed.reading.explanation.host.pressure,
                             gate.PressureUnavailable("PSI unreadable"))

    def test_unowned_scope_activity_round_trips(self) -> None:
        from scripts.build_queue.scope_activity import ScopeProcesses, NoLiveBuildRun

        activity = ScopeProcesses(2, "session", "worker", "/tree", "python3", NOW, False, NoLiveBuildRun())
        current = gate.UnownedSliceMemory(GIB, owner="scope", activity=activity, pid=gate.PidNotRecorded())
        self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(current), gate.UnownedSliceMemory), current)

    def test_service_job_state_and_separate_swap_survive_the_view_reply(self) -> None:
        from scripts.build_queue.scope_activity import IdleService, RunningServiceJob
        from scripts.build_queue.queue_fixture import queue_read_fixture

        for state in (IdleService(), RunningServiceJob(NOW - 120)):
            with self.subTest(state=state):
                memory = gate.UnownedSliceMemory(GIB // 10, swap_bytes=2 * GIB,
                    owner="hana-ci.slice", job_state=state, pid=gate.PidNotRecorded())
                step = gate.RunningStep("", "", "unowned", "ci.slice", 0, memory, pid=gate.PidNotRecorded())
                view = gate.QueueView((), (step,), 20 * GIB, 0, 3 * GIB, NOW)
                reading = queue_read_fixture(view, policy=admission_policy_fixture())
                reply = wire.ViewReply(reading)
                parsed = wire.parse_reply(wire.encode_reply(reply))
                self.assertEqual(parsed, reply)
                assert isinstance(parsed, wire.ViewReply)
                shown = queue_evidence.running_text(parsed.reading.explanation.running[0],
                    parsed.reading.explanation, {}, ZoneInfo("UTC"))
                self.assertIn("0.1 GiB RAM; 2.0 GiB swapped", shown)
                self.assertIn("running job, 2 min" if isinstance(state, RunningServiceJob) else "idle", shown)

    def test_main_display_fields_and_diagnostics_remain_readable(self) -> None:
        from scripts.build_queue.scope_activity import LiveBuildRun
        from scripts.build_queue.queue_fixture import queue_read_fixture

        view = gate.QueueView((), (), 0, 0, 0, NOW)
        reading = queue_read_fixture(view, policy=admission_policy_fixture())
        current = cast(dict[str, object], codec.snapshot_value(reading))
        del current["diagnostics"]
        current["diagnostics"] = [{"kind": "ConfigurationDiagnostic", "diagnostic": "service warning"}]
        parsed = codec.parse_snapshot_value(current, gate.QueueRead)
        self.assertEqual(parsed, replace(reading, diagnostics=(gate.ConfigurationDiagnostic("service warning"),)))
        step = gate.RunningStep("owner", "worker", "merge-gate", "/tree", GIB, gate.MeasuredMemory(0),
                                run=LiveBuildRun(123, "456", NOW - 60), started_at=NOW - 30, pid=123)
        self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(step), gate.RunningStep), step)

    def test_unit_override_round_trips(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            original_launch = fixture.launch()
            for request in (original, original_launch):
                with self.subTest(request=type(request).__name__):
                    current = replace(request, identity=replace(request.identity, unit="queue-unit"))
                    self.assertEqual(wire.parse_request(wire.encode_request(current)), current)
                    self.assertEqual(wire.parse_record(wire.encode_record(current), type(current)), current)

    def test_shell_and_protocol_share_a_version_that_includes_the_version_module(self) -> None:
        with AdmissionFixture() as fixture:
            source = Path(__file__).resolve().parents[1]
            scripts = fixture.root / "scripts"
            for relative in (*admission_code.ADMISSION_CODE_PATHS, "build_hold/override_guard.sh", "lib/py"):
                target = scripts / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_bytes((source / relative).read_bytes())
                target.chmod((source / relative).stat().st_mode)
            before = wire.admission_code_version(scripts)
            environment = {key: value for key, value in os.environ.items() if key != "BUILDLOG_ADMISSION_CODE_VERSION"}
            command = '''BUILDLOG_ADMISSION_CODE_VERSION="$("$4" "$3" code-version)" || exit 2
export BUILDLOG_ADMISSION_CODE_VERSION
source "$1"
printf '%s\\n' "$BUILDLOG_ADMISSION_CODE_VERSION"
printf '\\n# Changed version code.\\n' >> "$2"
source "$1"
printf '%s\\n' "$BUILDLOG_ADMISSION_CODE_VERSION"
'''
            result = subprocess.run(["bash", "-c", command, "bash", str(scripts / "lint/memory_gate.sh"),
                                     str(scripts / "build_queue/admission_code.py"),
                                     str(source / "build_queue/admission_client.py"), str(source / "lib/py")],
                                    env=environment, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [before, before])
            self.assertNotEqual(wire.admission_code_version(scripts), before)


    def test_admission_round_trips_with_no_run_and_an_explicit_run_identity(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            self.assertEqual(wire.parse_request(wire.encode_request(request)), request)
            run = wire.RunIdentity(123, "456")
            run_request = replace(request, run=run)
            self.assertEqual(wire.parse_request(wire.encode_request(run_request)), run_request)
            run_encoded = cast(dict[str, object], json.loads(wire.encode_request(run_request)))
            self.assertEqual(cast(dict[str, object], run_encoded["data"])["run"], codec.snapshot_value(run))

    def test_pressure_condition_variants_keep_their_meaning_through_the_codec(self) -> None:
        conditions: tuple[gate.PressureConditionNotRecorded | Literal["full", "swap"], ...] = (
            "full", "swap", gate.PressureConditionNotRecorded())
        for condition in conditions:
            with self.subTest(condition=condition):
                pressure = gate.MemoryPressure(8, 15, 80, condition=condition)
                reply = wait_reply(pressure)
                reply = replace(reply, snapshot=replace(reply.snapshot,
                    decision=replace(reply.snapshot.decision, pressure=pressure)))
                self.assertEqual(wire.parse_reply(wire.encode_reply(reply)), reply)


    def test_every_request_round_trips_with_profiles_width_and_call_states(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            requests: list[wire.Request] = [request, fixture.launch(),
                replace(request, profile=gate.ProgramProfile("merge-gate", "repo", "tree", gate.HistoryPeakSizing(3 * GIB, gate.FirstProgramPeak(3 * GIB), 0.0))),
                replace(request, profile=gate.ProgramProfile("rust-analyzer", "repo", "tree",
                    gate.HistoryPeakSizing(18 * GIB, gate.FirstProgramPeak(18 * GIB), NOW))),
                replace(request, profile=gate.ProgramProfile("rust-analyzer", "repo", "tree",
                    gate.HistoryPeakSizing(18 * GIB, gate.MeasuredProgramPeak(7 * GIB, 5), NOW))),
                wire.ReleaseRequest("release", "/tmp/stem", 137),
                wire.WidthRequest("width", "/tmp/stem", 4), wire.ViewRequest(),
                wire.ExplainRequest(), wire.MineRequest("session"),
                wire.AdmitHeadRequest("head", "urgent", "actor"),
                wire.PrioritySetRequest("priority", "queue-unit", "urgent", "actor"),
                wire.PriorityClearRequest("clear", "actor"), wire.TokenWaitRequest("token", "stem")]
            for width in (gate.RequestedTestWidth(3), gate.UnspecifiedTestWidth(), gate.NoTestWidth()):
                for call in (gate.CallRecord("a-call"), gate.NoCallRecord()):
                    requests.append(replace(request, test_width=width, call=call))
            for value in requests:
                with self.subTest(request=value):
                    encoded = wire.encode_request(value)
                    self.assertTrue(encoded.endswith("\n"))
                    self.assertEqual(len(encoded.splitlines()), 1)
                    self.assertEqual(wire.parse_request(encoded), value)
                    self.assertEqual(wire.parse_record(wire.encode_record(value), type(value)), value)

    def test_every_reply_round_trips_and_domain_payloads_use_the_shared_codec(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request())
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
            replies: tuple[wire.Reply, ...] = (
                wait_reply(gate.EstimateExpired(NOW - admission_policy_fixture().timing.estimate_refresh_seconds), 1, "head", "estimate expired"),
                admitted_reply("stem", GIB, "future-source", "stem.max", "stem.wave"),
                wire.LaunchAdmitted(GIB, "future-source"), wire.Refused("no"), wire.Ok(),
                wire.ViewReply(reading), wire.ExplainReply(reading.explanation), wire.MineReply(reading))
            for reply in replies:
                with self.subTest(reply=type(reply).__name__):
                    with mock.patch.object(codec, "snapshot_value", wraps=codec.snapshot_value) as encode:
                        line = wire.encode_reply(reply)
                    self.assertGreater(encode.call_count, 0)
                    with mock.patch.object(codec, "parse_snapshot_value", wraps=codec.parse_snapshot_value) as parse:
                        self.assertEqual(wire.parse_reply(line), reply)
                    self.assertGreater(parse.call_count, 0)

    def test_admit_and_launch_have_distinct_outcomes(self) -> None:
        with AdmissionFixture() as fixture:
            admit, launch = fixture.request(), fixture.launch()
            held = wait_reply(gate.ReadyForAdmission(), 1, "head", "ready")
            admitted = admitted_reply("stem", GIB, "arbitrary source", "stem.max", "stem.wave")
            launched = wire.LaunchAdmitted(GIB, "arbitrary source")
            self.assertEqual(wire.parse_reply(wire.encode_reply(admitted), admit), admitted)
            self.assertEqual(wire.parse_reply(wire.encode_reply(launched), launch), launched)
            for request, reply in ((launch, admitted), (admit, launched)):
                with self.subTest(request=type(request).__name__), self.assertRaises(ValueError):
                    _ = wire.parse_reply(wire.encode_reply(reply), request)
            self.assertEqual(wire.parse_reply(wire.encode_reply(held), launch), held)
            fields = cast(dict[str, object], json.loads(wire.encode_reply(launched)))
            data = cast(dict[str, object], fields["data"])
            self.assertNotIn("reservation", data)
            self.assertNotIn("max_path", data)
            self.assertNotIn("wave_path", data)

    def test_unknown_versions_and_malformed_envelopes_are_rejected(self) -> None:
        message = cast(dict[str, object], json.loads(wire.encode_request(wire.ViewRequest())))
        message["v"] = 2
        with self.assertRaisesRegex(ValueError, "client version 2, service supports 1..1"):
            _ = wire.parse_request(json.dumps(message))
        for line in ("", "not json", "[]", '{}', '{"v":true}', '{"v":1,"type":"bump","data":{}}'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                _ = wire.parse_request(line)
        with self.assertRaises(ValueError):
            _ = wire.parse_reply('{"v":1,"type":"admitted","data":{}}')

    def test_socket_override_is_read_at_use(self) -> None:
        with mock.patch.dict(os.environ, {"ADMISSION_SOCKET": "/tmp/phase5-one.sock"}):
            self.assertEqual(wire.admission_socket_path(), Path("/tmp/phase5-one.sock"))
            os.environ["ADMISSION_SOCKET"] = "/tmp/phase5-two.sock"
            self.assertEqual(wire.admission_socket_path(), Path("/tmp/phase5-two.sock"))

    def test_expired_explanation_keeps_release_advice_and_decided_from_over_wire(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request(age=admission_policy_fixture().timing.estimate_refresh_seconds))
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            inputs = replace(inputs, spacing=gate.NextStartAt(NOW + 30))
            local = gate.queue_read_from(inputs, policy=admission_policy_fixture()).explanation
            parsed = wire.parse_reply(wire.encode_reply(wire.ExplainReply(local)))
            assert isinstance(parsed, wire.ExplainReply)
            self.assertEqual(parsed.explanation, local)
            self.assertEqual(parsed.explanation.decided_from, inputs)
            self.assertIsInstance(gate.smallest_head_release(parsed.explanation), gate.EstimateExpired)
            zone = ZoneInfo("UTC")
            self.assertEqual(queue_evidence.release_text(gate.smallest_head_release(parsed.explanation), parsed.explanation, {}, zone),
                             queue_evidence.release_text(gate.smallest_head_release(local), local, {}, zone))
            self.assertTrue(queue_evidence.release_text(gate.smallest_head_release(parsed.explanation), parsed.explanation, {}, zone).startswith("the estimate expired at"))
            assert isinstance(local.head, gate.HeadExplanation)
            assert isinstance(local.head.cause, gate.EstimateExpired)
            assert isinstance(parsed.explanation.head, gate.HeadExplanation)
            assert isinstance(parsed.explanation.head.cause, gate.EstimateExpired)
            self.assertEqual(queue_evidence.estimate_refresh_text(parsed.explanation.head.cause, zone),
                             queue_evidence.estimate_refresh_text(local.head.cause, zone))
