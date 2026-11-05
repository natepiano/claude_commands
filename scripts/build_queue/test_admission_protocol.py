"""The wire transports admission's domain records without another representation."""
from __future__ import annotations

from scripts.lint.memory_test_support import admission_policy_fixture

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import unittest
from typing import Literal, cast
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.lint import memory_admit as gate
from scripts.build_queue import admission_protocol as wire, queue_evidence
from scripts.build_queue import admission_code
from scripts.build_queue.admission_test_support import admitted_reply, wait_reply
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, NOW


class ProtocolTests(unittest.TestCase):

    def test_old_unavailable_cap_reply_keeps_the_room_without_an_empty_run_list(self) -> None:
        from scripts.build_queue.queue_fixture import QueueFixture

        with QueueFixture() as fixture:
            reading = gate.queue_read_from(fixture.inputs(policy=admission_policy_fixture()),
                                          gate.AdmissionLimits(3 * GIB), admission_policy_fixture())
            original_room = reading.explanation.room
            assert isinstance(original_room, gate.MemoryRoom)
            envelope = cast(dict[str, object], json.loads(wire.encode_reply(wire.ViewReply(reading))))
            data = cast(dict[str, object], envelope["data"])
            recorded = cast(dict[str, object], data["reading"])
            explanation = cast(dict[str, object], recorded["explanation"])
            room = cast(dict[str, object], explanation["room"])
            room["cap"] = {"kind": "CapUnavailable", "reason": "anonymous use unavailable"}

            reply = wire.parse_reply(json.dumps(envelope))
            assert isinstance(reply, wire.ViewReply)
            decoded_room = reply.reading.explanation.room
            assert isinstance(decoded_room, gate.MemoryRoom)
            self.assertIsInstance(decoded_room.cap, gate.CapUnavailable)
            self.assertEqual((decoded_room.available, decoded_room.room, decoded_room.kept),
                             (original_room.available, original_room.room, original_room.kept))
            shown = queue_evidence.room_text(decoded_room)
            self.assertTrue(shown.startswith(f"Cap unavailable; free {queue_evidence.gib(original_room.available)}"), shown)
            self.assertNotIn("anonymous use", shown)
            self.assertNotIn("unavailable for", shown)


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
            reading = gate.queue_read_from(inputs, gate.AdmissionLimits(3 * GIB),
                                           admission_policy_fixture())
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

    def test_unowned_scope_activity_round_trips_and_old_records_remain_readable(self) -> None:
        from scripts.build_queue.scope_activity import ScopeProcesses, NoLiveBuildRun

        activity = ScopeProcesses(2, "session", "worker", "/tree", "python3", NOW, False, NoLiveBuildRun())
        current = gate.UnownedSliceMemory(GIB, owner="scope", activity=activity, pid=gate.PidNotRecorded())
        self.assertEqual(gate.parse_snapshot_value(gate.snapshot_value(current), gate.UnownedSliceMemory), current)
        old = cast(dict[str, object], gate.snapshot_value(current))
        del old["activity"]
        self.assertEqual(gate.parse_snapshot_value(old, gate.UnownedSliceMemory),
                         gate.UnownedSliceMemory(GIB, owner="scope", pid=gate.PidNotRecorded()))

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
        current = cast(dict[str, object], gate.snapshot_value(reading))
        del current["diagnostics"]
        current["diagnostics"] = [{"kind": "ConfigurationDiagnostic", "diagnostic": "service warning"}]
        parsed = gate.parse_snapshot_value(current, gate.QueueRead)
        self.assertEqual(parsed, replace(reading, diagnostics=(gate.ConfigurationDiagnostic("service warning"),)))
        step = gate.RunningStep("owner", "worker", "merge-gate", "/tree", GIB, gate.MeasuredMemory(0),
                                run=LiveBuildRun(123, "456", NOW - 60), started_at=NOW - 30, pid=123)
        self.assertEqual(gate.parse_snapshot_value(gate.snapshot_value(step), gate.RunningStep), step)
        old = cast(dict[str, object], gate.snapshot_value(step))
        for name in ("unit", "run", "started_at"):
            del old[name]
        self.assertEqual(gate.parse_snapshot_value(old, gate.RunningStep),
                         gate.RunningStep("owner", "worker", "merge-gate", "/tree", GIB, gate.MeasuredMemory(0), pid=123))

    def test_unit_override_round_trips_and_main_encoder_omits_it(self) -> None:
        with AdmissionFixture() as fixture:
            original = fixture.request()
            original_launch = fixture.launch()
            for request in (original, original_launch):
                with self.subTest(request=type(request).__name__):
                    current = replace(request, identity=replace(request.identity, unit="queue-unit"))
                    self.assertEqual(wire.parse_request(wire.encode_request(current)), current)
                    self.assertEqual(wire.parse_record(wire.encode_record(current), type(current)), current)
                    compatible = wire.encode_request(current, main_service=True)
                    self.assertEqual(json.loads(compatible), json.loads(wire.encode_request(request, main_service=True)))
                    envelope = cast(dict[str, object], json.loads(compatible))
                    data = cast(dict[str, object], envelope["data"])
                    self.assertNotIn("unit", cast(dict[str, object], data["identity"]))
                    restored = wire.parse_request(compatible)
                    assert isinstance(restored, (wire.AdmitRequest, wire.LaunchRequest))
                    self.assertEqual(restored.identity, request.identity)
    def test_shell_and_protocol_share_a_version_that_includes_the_version_module(self) -> None:
        with AdmissionFixture() as fixture:
            source = Path(__file__).resolve().parents[1]
            scripts = fixture.root / "scripts"
            for relative in (*admission_code.ADMISSION_CODE_PATHS, "build_hold/override_guard.sh"):
                target = scripts / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_bytes((source / relative).read_bytes())
            before = wire.admission_code_version(scripts)
            environment = {key: value for key, value in os.environ.items() if key != "BUILDLOG_ADMISSION_CODE_VERSION"}
            command = '''BUILDLOG_ADMISSION_CODE_VERSION="$(python3 "$3" code-version)" || exit 2
export BUILDLOG_ADMISSION_CODE_VERSION
source "$1"
printf '%s\\n' "$BUILDLOG_ADMISSION_CODE_VERSION"
printf '\\n# Changed version code.\\n' >> "$2"
source "$1"
printf '%s\\n' "$BUILDLOG_ADMISSION_CODE_VERSION"
'''
            result = subprocess.run(["bash", "-c", command, "bash", str(scripts / "lint/memory_gate.sh"),
                                     str(scripts / "build_queue/admission_code.py"),
                                     str(source / "build_queue/admission_client.py")],
                                    env=environment, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [before, before])
            self.assertNotEqual(wire.admission_code_version(scripts), before)

    def test_main_service_running_steps_keep_their_identity_and_memory(self) -> None:
        raw = (Path(__file__).with_name("fixtures") / "live_main_view.json").read_bytes()
        reply = wire.parse_reply(raw, wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        running = reply.reading.view.running
        recorded = cast(dict[str, object], json.loads(raw))
        data = cast(dict[str, object], recorded["data"])
        reading = cast(dict[str, object], data["reading"])
        view = cast(dict[str, object], reading["view"])
        source = cast(list[dict[str, object]], view["running"])
        self.assertEqual(len(running), len(source))
        self.assertTrue(running)
        for step, original in zip(running, source, strict=True):
            self.assertEqual((step.session_id, step.session_name, step.step, step.folder, step.promise),
                             tuple(original[key] for key in ("session_id", "session_name", "step", "folder", "promise")))
            measured = cast(dict[str, object], original["measured"])
            self.assertEqual(cast(dict[str, object], gate.snapshot_value(step.measured))["bytes"], measured["bytes"])

    def test_main_service_unowned_memory_keeps_its_charge_and_scope(self) -> None:
        raw = (Path(__file__).with_name("fixtures") / "live_main_explain.json").read_bytes()
        reply = wire.parse_reply(raw, wire.ExplainRequest())
        assert isinstance(reply, wire.ExplainReply)
        unowned = [row for row in reply.explanation.running if isinstance(row.step.measured, gate.UnownedSliceMemory)]
        self.assertTrue(unowned)
        for row in unowned:
            memory = row.step.measured
            assert isinstance(memory, gate.UnownedSliceMemory)
            self.assertEqual(row.measured, memory.bytes)
            self.assertGreaterEqual(row.charged, memory.bytes)
            self.assertTrue(memory.cgroup.endswith(memory.owner))

    def test_no_run_admission_uses_main_fields_but_run_identity_remains_explicit(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            encoded = cast(dict[str, object], json.loads(wire.encode_request(request)))
            data = cast(dict[str, object], encoded["data"])
            self.assertNotIn("run", data)
            self.assertEqual(wire.parse_request(wire.encode_request(request)), request)
            run = wire.RunIdentity(123, "456")
            run_request = replace(request, run=run)
            self.assertEqual(wire.parse_request(wire.encode_request(run_request)), run_request)
            run_encoded = cast(dict[str, object], json.loads(wire.encode_request(run_request)))
            self.assertEqual(cast(dict[str, object], run_encoded["data"])["run"], gate.snapshot_value(run))

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

    def test_main_service_requests_omit_only_new_fields_and_preserve_every_main_field(self) -> None:
        # Main's encoder produced these records before the admission cut-over.
        lines = (Path(__file__).with_name("fixtures") / "main_request_records.jsonl").read_text().splitlines()
        for line in lines:
            with self.subTest(request=cast(dict[str, object], json.loads(line))["type"]):
                original = wire.parse_request(line)
                current = replace(original, code_version="current-caller-version")
                self.assertEqual(wire.parse_request(wire.encode_request(current)), current)
                compatible = wire.encode_request(current, main_service=True)
                self.assertEqual(json.loads(compatible), json.loads(line))
                self.assertEqual(wire.parse_request(compatible), original)

    def test_main_service_configuration_diagnostic_keeps_the_recorded_text(self) -> None:
        raw = cast(dict[str, object], json.loads(
            (Path(__file__).with_name("fixtures") / "main_pressure_wait.json").read_text()))
        data = cast(dict[str, object], raw["data"])
        snapshot = cast(dict[str, object], data["snapshot"])
        message = "main service recorded a configuration warning"
        snapshot["fallbacks"] = [{"kind": "ConfigFallback", "diagnostic": message}]
        reply = wire.parse_reply(json.dumps(raw))
        assert isinstance(reply, wire.Wait)
        self.assertEqual(reply.snapshot.diagnostics, (gate.ConfigurationDiagnostic(message),))

    def test_every_request_round_trips_with_profiles_width_and_call_states(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            requests: list[wire.Request] = [request, fixture.launch(),
                replace(request, profile=gate.ProgramProfile("merge-gate", "repo", "tree", gate.FixedCapSizing(3 * GIB))),
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
            inputs, limits = fixture.server.last_inputs, fixture.server.last_limits
            assert isinstance(inputs, gate.QueueInputs)
            assert isinstance(limits, gate.AdmissionLimits)
            reading = gate.queue_read_from(inputs, limits, policy=admission_policy_fixture())
            replies: tuple[wire.Reply, ...] = (
                wait_reply(gate.EstimateExpired(NOW - admission_policy_fixture().timing.estimate_refresh_seconds), 1, "head", "estimate expired"),
                admitted_reply("stem", GIB, "future-source", "stem.max", "stem.wave"),
                wire.LaunchAdmitted(GIB, "future-source"), wire.Refused("no"), wire.Ok(),
                wire.ViewReply(reading), wire.ExplainReply(reading.explanation), wire.MineReply(reading))
            for reply in replies:
                with self.subTest(reply=type(reply).__name__):
                    with mock.patch.object(gate, "snapshot_value", wraps=gate.snapshot_value) as encode:
                        line = wire.encode_reply(reply)
                    self.assertGreater(encode.call_count, 0)
                    with mock.patch.object(gate, "parse_snapshot_value", wraps=gate.parse_snapshot_value) as parse:
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
            inputs, limits = fixture.server.last_inputs, fixture.server.last_limits
            assert isinstance(inputs, gate.QueueInputs)
            assert isinstance(limits, gate.AdmissionLimits)
            inputs = replace(inputs, recovery=gate.NextStartAt(NOW + 30))
            local = gate.queue_read_from(inputs, limits, policy=admission_policy_fixture()).explanation
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
