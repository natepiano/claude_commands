"""The wire transports admission's domain records without another representation."""
from __future__ import annotations

from scripts.lint.memory_test_support import admission_policy_fixture

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import unittest
from typing import cast
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.lint import memory_admit as gate
from scripts.build_queue import admission_protocol as wire, queue_evidence
from scripts.build_queue import admission_code
from scripts.build_queue.admission_test_support import admitted_reply, wait_reply
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, NOW


class ProtocolTests(unittest.TestCase):
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
            command = '''source "$1"
printf '%s\\n' "$BUILDLOG_ADMISSION_CODE_VERSION"
printf '\\n# Changed version code.\\n' >> "$2"
source "$1"
printf '%s\\n' "$BUILDLOG_ADMISSION_CODE_VERSION"
'''
            result = subprocess.run(["bash", "-c", command, "bash", str(scripts / "lint/memory_gate.sh"),
                                     str(scripts / "build_queue/admission_code.py")],
                                    env=environment, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [before, before])
            self.assertNotEqual(wire.admission_code_version(scripts), before)

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
                wait_reply(gate.EstimateExpired(NOW - admission_policy_fixture().timing.head_seconds), 1, "head", "estimate expired"),
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
            _ = fixture.server.handle_request(fixture.request(age=admission_policy_fixture().timing.head_seconds))
            inputs, limits = fixture.server.last_inputs, fixture.server.last_limits
            assert isinstance(inputs, gate.QueueInputs)
            assert isinstance(limits, gate.AdmissionLimits)
            inputs = replace(inputs, recovery=gate.RecoveryStarts(NOW + 120, NOW + 30))
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
            assert isinstance(local.head.waiter.reason, gate.EstimateExpired)
            assert isinstance(parsed.explanation.head, gate.HeadExplanation)
            assert isinstance(parsed.explanation.head.waiter.reason, gate.EstimateExpired)
            self.assertEqual(queue_evidence.estimate_refresh_text(parsed.explanation.head.waiter.reason, zone),
                             queue_evidence.estimate_refresh_text(local.head.waiter.reason, zone))
