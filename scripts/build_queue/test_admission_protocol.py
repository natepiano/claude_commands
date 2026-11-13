"""The wire transports admission's domain records without another representation."""
from __future__ import annotations

from scripts.lib import test_phase

from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.admission_protocol_v1_fixture import parse_request as decode_legacy

from scripts.lint.memory_test_support import admission_policy_fixture

from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import Literal, cast
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.lib.build_steps import read_step_names
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit as gate
from scripts.build_queue import admission_client as client, admission_protocol as wire, queue_evidence
from scripts.build_queue import admission_code, admission_records as records
from scripts.build_queue import placement, queue_snapshot
from scripts.build_queue.admission_test_support import admitted_reply, wait_reply
from scripts.build_queue.admission_test_support import AdmissionFixture, Exchange, ScriptedTransport, GIB, NOW


class ProtocolTests(unittest.TestCase):

    def test_clear_pressure_measurements_survive_the_codec_and_reader(self) -> None:
        for measurements in (records.PressureMeasurements(1.25, 2.5, 62.9), records.PressureMeasurementsNotRecorded()):
            with self.subTest(measurements=measurements):
                pressure = records.NoPressureHold(measurements)
                encoded = codec.snapshot_value(pressure)
                self.assertEqual(codec.parse_snapshot_value(encoded, records.Pressure), pressure)
                reading = gate.queue_read_from(
                    records.QueueInputs([], (), (), records.HostMemory(30 * GIB, 60 * GIB), pressure,
                        records.ReadyToStart(), records.NoHeadAdmissionOverride(), NOW), admission_policy_fixture())
                raw = codec.snapshot_value(reading)
                decoded = codec.parse_snapshot_value(raw, records.QueueRead)
                self.assertEqual(decoded, reading)
                with tempfile.TemporaryDirectory() as folder:
                    directory = Path(folder)
                    queue_snapshot.write_queue_snapshot(directory, reading.explanation.decided_from, admission_policy_fixture())
                    saved = queue_snapshot.read_queue_snapshot(directory)
                    assert isinstance(saved, queue_snapshot.QueueSnapshot)
                    self.assertEqual(saved.inputs.pressure, pressure)
                    path = directory / "queue.json"
                    old = cast(dict[str, dict[str, dict[str, object]]], json.loads(path.read_text()))
                    del old["inputs"]["pressure"]["measurements"]
                    _ = path.write_text(json.dumps(old))
                    self.assertIsInstance(queue_snapshot.read_queue_snapshot(directory), queue_snapshot.NoQueueSnapshot)

        with self.assertRaises(ValueError):
            _ = codec.parse_snapshot_value({"kind": "NoPressureHold"}, records.Pressure)

    def test_client_startup_imports_only_the_wire_without_record_readers(self) -> None:
        for operation in ("import", "release reply"):
            with self.subTest(operation=operation):
                source = "import json, sys; from scripts.build_queue import admission_client; "
                if operation == "release reply":
                    source += (
                        "wire = admission_client.wire; "
                        "reply = wire.parse_reply(wire.encode_reply(wire.Ok()), wire.ReleaseRequest('request', 'reservation', 0)); "
                        "assert isinstance(reply, wire.Ok); ")
                source += "print(json.dumps(sorted(sys.modules)))"
                result = subprocess.run([sys.executable, "-c", source],
                    cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                    timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                loaded = cast(list[str], json.loads(result.stdout))
                self.assertIn("scripts.build_queue.admission_wire", loaded)
                for module in (
                    "scripts.build_queue.admission_protocol", "scripts.build_queue.admission_records",
                    "scripts.build_queue.queue_lines", "scripts.build_queue.progress_profile",
                    "scripts.build_queue.scope_activity", "scripts.build_queue.cloud_progress",
                    "scripts.buildlog.store", "scripts.lib.build_steps", "scripts.lib.user_zone",
                    "scripts.message.send_budget", "scripts.message.send_result", "scripts.message.session_send",
                ):
                    with self.subTest(module=module):
                        self.assertNotIn(module, loaded)

    def test_local_and_cloud_places_survive_the_request_wire(self) -> None:
        with AdmissionFixture() as fixture:
            for place in (wire.LocalPlace(), wire.CloudPlace("aws")):
                with self.subTest(place=place):
                    request = replace(fixture.request(step="merge-gate"), place=place)
                    parsed = wire.parse_request(wire.encode_request(request))
                    self.assertEqual(parsed, request)
                    assert isinstance(parsed, wire.AdmitRequest)
                    self.assertEqual(parsed.place, place)
                    for version in (1, 2):
                        envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
                        envelope["v"] = version
                        self.assertEqual(wire.parse_request_envelope(json.dumps(envelope)), (version, request))

    def test_placement_exchanges_preserve_identity_without_replaying_authority(self) -> None:
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            request = placement_request(fixture)
            destination = request.constraints.candidates[0].destination
            assignment = placement.PlacementAssignment(request.episode, "attempt-a", destination,
                request.constraints.source_key, request.owner, request.constraints.session, request.since)
            claim = placement.ProviderClaimReference("mac", destination.target, destination.scope,
                                                     "claim-a", assignment.episode, assignment.attempt)
            requests: tuple[wire.Request, ...] = (
                request, wire.ClaimFailure("failed", assignment, "busy"),
                wire.ConsumePermission("consume", assignment, claim),
                wire.ExecutionReport("report", assignment, claim, placement.ExecutionNotStarted()),
                wire.ExecutionReport("report-observed", assignment, claim, placement.ObservedExecution(
                    placement.ProcessExecution("fixture-mac", "generation-a", 42, "remote-start"))),
                wire.ExecutionReport("report-unavailable", assignment, claim,
                    placement.ExecutionEvidenceUnavailable("unreachable", fixture.clock.now())),
            )
            for message in requests:
                with self.subTest(message=message):
                    self.assertEqual(wire.parse_request(wire.encode_request(message)), message)
            replies: tuple[wire.Reply, ...] = (
                wire.PlacementWait(request.episode, request.since, 1,
                    (placement.CandidateEligibility(destination, placement.TemporaryExclusion("busy")),)),
                wire.Assignment(assignment), wire.FirstAuthorization(assignment, claim),
                wire.DispatchStatus(assignment, placement.CurrentAssignment(), placement.PermissionAvailable()),
                wire.DispatchStatus(assignment, placement.CurrentAssignment(), placement.PermissionConsumed(fixture.clock.now())),
                wire.DispatchStatus(assignment, placement.ClosedAssignment("obsolete"), placement.NoLaunchPermission("closed")),
                wire.DispatchStatus(placement.UnknownAssignment(assignment.episode, "old-attempt"),
                    placement.ClosedAssignment("unknown"), placement.NoLaunchPermission("unknown")),
            )
            for reply in replies:
                with self.subTest(reply=reply):
                    self.assertEqual(wire.parse_reply(wire.encode_reply(reply)), reply)

    def test_cloud_rows_survive_view_and_mine_reply_wires(self) -> None:
        from scripts.build_queue import cloud_progress, progress_profile

        with AdmissionFixture() as fixture:
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            base = reply.reading
            cloud_row = wire.CloudRow(wire.Owner(700001, "10"),
                wire.Identity("cloud-session", "cloud-worker", "merge-gate", "/private", "cloud-unit"),
                wire.CloudPlace("aws"), NOW - 60, NOW - 50,
                cloud_progress.CloudProgress("running", 32, 1, NOW - 20),
                progress_profile.ProgressCounter("modules", 7, 12), records.ReadyForAdmission())
            reading = wire.CloudQueueRead(base.view, base.explanation, base.ordered,
                diagnostics=base.diagnostics, cloud=(cloud_row,), cloud_slots=9, cloud_gate_stale_pushes=3)
            for outcome in (wire.ViewReply(reading), wire.MineReply(reading)):
                with self.subTest(reply=type(outcome).__name__):
                    self.assertEqual(wire.parse_reply(wire.encode_reply(outcome)), outcome)

    def test_cloud_slot_wait_survives_the_reply_wire(self) -> None:
        reply = replace(wait_reply(records.NeedsMemory()),
            reason=wire.CloudSlotWait(9, 9), text="waits for a cloud slot: 9 of 9 in use")
        self.assertEqual(wire.parse_reply(wire.encode_reply(reply)), reply)

    def test_a_request_without_place_defaults_to_local(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            self.assertEqual(request.place, wire.LocalPlace())
            envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
            data = cast(dict[str, object], envelope["data"])
            del data["place"]
            parsed = wire.parse_request(json.dumps(envelope) + "\n")
            self.assertEqual(parsed, request)

    def test_codec_reuses_shared_records_only_within_one_encode(self) -> None:
        shared = {"name": "private", "values": [1, 2]}
        encoded = cast(list[object], codec.snapshot_value([shared, shared]))
        self.assertEqual(encoded, [shared, shared])
        self.assertIs(encoded[0], encoded[1])
        shared["name"] = "changed"
        again = cast(list[object], codec.snapshot_value([shared, shared]))
        self.assertEqual(again, [shared, shared])
        self.assertIsNot(again[0], encoded[0])
        for scalar in (None, True, 3, 2.5, "private"):
            self.assertIs(codec.snapshot_value(scalar), scalar)

    def test_reply_bytes_keep_the_existing_domain_representation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant = fixture.server.handle_request(request)[0]
            viewed = fixture.server.handle_request(wire.ViewRequest())[0]
            explained = fixture.server.handle_request(wire.ExplainRequest())[0]
            for reply, name in ((grant, "admitted"), (viewed, "view"), (explained, "explain")):
                with self.subTest(name=name):
                    expected = json.dumps({"v": wire.VERSION, "type": name,
                        "data": codec.snapshot_value(reply)}, separators=(",", ":"),
                        ensure_ascii=False, allow_nan=False) + "\n"
                    self.assertEqual(wire.encode_reply(reply), expected)
                    self.assertEqual(wire.parse_reply(expected), reply)
            self.assertIsInstance(grant, wire.Admitted)

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
                measured=records.MeasuredMemory(2 * GIB, swap_bytes=4 * GIB,
                                            anonymous=records.AnonymousRunUse(2 * GIB)))
            reservation = inputs.reservations[0].copy()
            reservation["need"] = 6 * GIB
            inputs = replace(inputs, running=(running,), reservations=(reservation,),
                host=records.HostMemory(10 * GIB, 60 * GIB),
                pressure=records.PressureUnavailable("PSI unreadable"))
            reading = gate.queue_read_from(inputs, admission_policy_fixture())
            reply = wire.ViewReply(reading)
            parsed = wire.parse_reply(wire.encode_reply(reply))
            self.assertEqual(parsed, reply)
            assert isinstance(parsed, wire.ViewReply)
            room = parsed.reading.explanation.room
            assert isinstance(room, records.MemoryRoom)
            self.assertEqual((room.room, room.kept, room.used, room.cap),
                             (3 * GIB, 4 * GIB, 2 * GIB, 9 * GIB))
            self.assertEqual(parsed.reading.view.running[0].measured, running.measured)
            self.assertIsInstance(parsed.reading.explanation.host, records.HostAvailable)
            assert isinstance(parsed.reading.explanation.host, records.HostAvailable)
            self.assertEqual(parsed.reading.explanation.host.pressure,
                             records.PressureUnavailable("PSI unreadable"))

    def test_unowned_scope_activity_round_trips(self) -> None:
        from scripts.build_queue.scope_activity import ScopeProcesses, NoLiveBuildRun

        activity = ScopeProcesses(2, "session", "worker", "/tree", "python3", NOW, False, NoLiveBuildRun())
        current = records.UnownedSliceMemory(GIB, owner="scope", activity=activity, pid=records.PidNotRecorded())
        self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(current), records.UnownedSliceMemory), current)

    def test_service_job_state_and_separate_swap_survive_the_view_reply(self) -> None:
        from scripts.build_queue.scope_activity import IdleService, RunningServiceJob
        from scripts.build_queue.queue_fixture import queue_read_fixture

        for state in (IdleService(), RunningServiceJob(NOW - 120)):
            with self.subTest(state=state):
                memory = records.UnownedSliceMemory(GIB // 10, swap_bytes=2 * GIB,
                    owner="hana-ci.slice", job_state=state, pid=records.PidNotRecorded())
                step = records.RunningStep("", "", "unowned", "ci.slice", 0, memory, pid=records.PidNotRecorded())
                view = records.QueueView((), (step,), 20 * GIB, 0, 3 * GIB, NOW)
                reading = queue_read_fixture(view, policy=admission_policy_fixture())
                reply = wire.ViewReply(reading)
                parsed = wire.parse_reply(wire.encode_reply(reply))
                self.assertEqual(parsed, reply)
                assert isinstance(parsed, wire.ViewReply)
                shown = queue_evidence.running_text(parsed.reading.explanation.running[0],
                    parsed.reading.explanation, {}, ZoneInfo("UTC"), names=read_step_names())
                self.assertIn("0.1 GiB RAM; 2.0 GiB swapped", shown)
                self.assertIn("running job, 2 min" if isinstance(state, RunningServiceJob) else "idle", shown)

    def test_main_display_fields_and_diagnostics_remain_readable(self) -> None:
        from scripts.build_queue.scope_activity import LiveBuildRun
        from scripts.build_queue.queue_fixture import queue_read_fixture

        view = records.QueueView((), (), 0, 0, 0, NOW)
        reading = queue_read_fixture(view, policy=admission_policy_fixture())
        current = cast(dict[str, object], codec.snapshot_value(reading))
        del current["diagnostics"]
        current["diagnostics"] = [{"kind": "ConfigurationDiagnostic", "diagnostic": "service warning"}]
        parsed = codec.parse_snapshot_value(current, records.QueueRead)
        self.assertEqual(parsed, replace(reading, diagnostics=(records.ConfigurationDiagnostic("service warning"),)))
        step = records.RunningStep("owner", "worker", "merge-gate", "/tree", GIB, records.MeasuredMemory(0),
                                run=LiveBuildRun(123, "456", NOW - 60), started_at=NOW - 30, pid=123)
        self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(step), records.RunningStep), step)

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
        conditions: tuple[records.PressureConditionNotRecorded | Literal["full", "swap"], ...] = (
            "full", "swap", records.PressureConditionNotRecorded())
        for condition in conditions:
            with self.subTest(condition=condition):
                pressure = records.MemoryPressure(8, 15, 80, condition=condition)
                reply = wait_reply(pressure)
                reply = replace(reply, snapshot=replace(reply.snapshot,
                    decision=replace(reply.snapshot.decision, pressure=pressure)))
                self.assertEqual(wire.parse_reply(wire.encode_reply(reply)), reply)


    def test_every_request_round_trips_with_profiles_width_and_call_states(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            requests: list[wire.Request] = [request, fixture.launch(),
                replace(request, profile=records.ProgramProfile("merge-gate", "repo", "tree", records.HistoryPeakSizing(3 * GIB, records.FirstProgramPeak(3 * GIB), 0.0))),
                replace(request, profile=records.ProgramProfile("rust-analyzer", "repo", "tree",
                    records.HistoryPeakSizing(18 * GIB, records.FirstProgramPeak(18 * GIB), NOW))),
                replace(request, profile=records.ProgramProfile("rust-analyzer", "repo", "tree",
                    records.HistoryPeakSizing(18 * GIB, records.MeasuredProgramPeak(7 * GIB, 5), NOW))),
                wire.ReleaseRequest("release", "/tmp/stem", 137),
                wire.WidthRequest("width", "/tmp/stem", 4), wire.ViewRequest(),
                wire.ExplainRequest(), wire.MineRequest("session"),
                wire.AdmitHeadRequest("head", "urgent", "actor"),
                wire.PrioritySetRequest("priority", "queue-unit", "urgent", "actor", records.SelectedPriorityStep(42, "start", 123)),
                wire.PriorityClearRequest("clear", "actor"), wire.TokenWaitRequest("token", "stem")]
            for width in (records.RequestedTestWidth(3), records.UnspecifiedTestWidth(), records.NoTestWidth()):
                for call in (test_phase.CallRecord("a-call"), test_phase.NoCallRecord()):
                    requests.append(replace(request, test_width=width, call=call))
            for value in requests:
                with self.subTest(request=value):
                    encoded = wire.encode_request(value)
                    self.assertTrue(encoded.endswith("\n"))
                    self.assertEqual(len(encoded.splitlines()), 1)
                    self.assertEqual(wire.parse_request(encoded), value)
                    self.assertEqual(wire.parse_record(wire.encode_record(value), type(value)), value)
            build = fixture.request(step="build")
            assert isinstance(build.profile, records.StepProfile)
            self.assertEqual(wire.VERSION, 3)
            for share in (0, 10, 50):
                shaped = replace(build, profile=replace(build.profile,
                    peak=records.ExpectedPeak(10 * GIB, "shape", 14, records.ShapeRankedPeak(14, share))))
                self.assertEqual(wire.parse_record(wire.encode_record(shaped), wire.AdmitRequest), shaped)
                for version in (1, 2):
                    with self.subTest(share=share, shape_version=version):
                        envelope = cast(dict[str, object], json.loads(wire.encode_request(shaped)))
                        envelope["v"] = version
                        data = cast(dict[str, object], envelope["data"])
                        profile = cast(dict[str, object], data["profile"])
                        peak = cast(dict[str, object], profile["peak"])
                        basis = cast(dict[str, object], peak["basis"])
                        if share == 10:
                            self.assertEqual(basis["kind"], "ShapeP90")
                            self.assertNotIn("share_percent", basis)
                            self.assertEqual(codec.snapshot_value(decode_legacy(json.dumps(envelope))), data)
                        else:
                            self.assertEqual((basis["kind"], basis["share_percent"]), ("ShapeRankedPeak", share))
                            with self.assertRaises(ValueError):
                                _ = decode_legacy(json.dumps(envelope))
                        self.assertEqual(wire.parse_request_envelope(json.dumps(envelope)), (version, shaped))
                        self.assertEqual(wire.parse_record(json.dumps(data), wire.AdmitRequest), shaped)

    def test_every_reply_round_trips_and_domain_payloads_use_the_shared_codec(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request())
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            reading = gate.queue_read_from(inputs, policy=admission_policy_fixture())
            replies: tuple[wire.Reply, ...] = (
                wait_reply(records.EstimateExpired(NOW - admission_policy_fixture().timing.estimate_refresh_seconds), 1, "head", "estimate expired"),
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
            held = wait_reply(records.ReadyForAdmission())
            for share in (0, 10, 50):
                shaped = replace(held, snapshot=replace(held.snapshot,
                    peak=records.ExpectedPeak(10 * GIB, "shape", 14, records.ShapeRankedPeak(14, share))))
                self.assertEqual(wire.parse_reply(wire.encode_reply(shaped)), shaped)
                for version in (1, 2):
                    with self.subTest(share=share, reply_version=version):
                        line = wire.encode_reply(shaped, version=version)
                        envelope = cast(dict[str, object], json.loads(line))
                        self.assertEqual(envelope["v"], version)
                        self.assertEqual(wire.parse_reply(line), shaped)
                        if share == 10:
                            self.assertEqual(envelope["v"], version)
                            self.assertIn('"ShapeP90"', line)
                            self.assertNotIn('"share_percent"', line)
                        else:
                            self.assertIn('"ShapeRankedPeak"', line)
                            self.assertIn(f'"share_percent":{share}', line)

    def test_admit_and_launch_have_distinct_outcomes(self) -> None:
        with AdmissionFixture() as fixture:
            admit, launch = fixture.request(), fixture.launch()
            held = wait_reply(records.ReadyForAdmission(), 1, "head", "ready")
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
        message["v"] = 4
        with self.assertRaisesRegex(ValueError, "client version 4, service supports 1..3"):
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
            assert isinstance(inputs, records.QueueInputs)
            inputs = replace(inputs, spacing=records.NextStartAt(NOW + 30))
            local = gate.queue_read_from(inputs, policy=admission_policy_fixture()).explanation
            parsed = wire.parse_reply(wire.encode_reply(wire.ExplainReply(local)))
            assert isinstance(parsed, wire.ExplainReply)
            self.assertEqual(parsed.explanation, local)
            self.assertEqual(parsed.explanation.decided_from, inputs)
            self.assertIsInstance(gate.smallest_head_release(parsed.explanation), records.EstimateExpired)
            zone = ZoneInfo("UTC")
            names = read_step_names()
            shown = queue_evidence.release_text(gate.smallest_head_release(parsed.explanation), parsed.explanation, {}, zone, names=names)
            self.assertEqual(shown,
                             queue_evidence.release_text(gate.smallest_head_release(local), local, {}, zone, names=names))
            self.assertTrue(shown.startswith("the estimate expired at"))
            assert isinstance(local.head, records.HeadExplanation)
            assert isinstance(local.head.cause, records.EstimateExpired)
            assert isinstance(parsed.explanation.head, records.HeadExplanation)
            assert isinstance(parsed.explanation.head.cause, records.EstimateExpired)
            self.assertEqual(queue_evidence.estimate_refresh_text(parsed.explanation.head.cause, zone),
                             queue_evidence.estimate_refresh_text(local.head.cause, zone))
