"""The wire transports admission's domain records without another representation."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from typing import cast
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.lint import memory_admit as gate
from scripts.build_queue import admission_protocol as wire, build_why
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, NOW


class ProtocolTests(unittest.TestCase):
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
                wire.AdmitHeadRequest("head", "urgent", "actor")]
            for width in (wire.RequestedTestWidth(3), wire.UnspecifiedTestWidth(), wire.NoTestWidth()):
                for call in (wire.CallRecord("a-call"), wire.NoCallRecord()):
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
            reading = gate.queue_read_from(inputs, limits, policy=gate.DEFAULT_ADMISSION_POLICY)
            replies: tuple[wire.Reply, ...] = (
                wire.Wait(gate.EstimateExpired(NOW - gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds), 1, "head", "estimate expired"),
                wire.Admitted("stem", GIB, "future-source", "stem.max", "stem.wave"),
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
            held = wire.Wait(gate.ReadyForAdmission(), 1, "head", "ready")
            admitted = wire.Admitted("stem", GIB, "arbitrary source", "stem.max", "stem.wave")
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
            _ = fixture.server.handle_request(fixture.request(age=gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds))
            inputs, limits = fixture.server.last_inputs, fixture.server.last_limits
            assert isinstance(inputs, gate.QueueInputs)
            assert isinstance(limits, gate.AdmissionLimits)
            inputs = replace(inputs, recovery=gate.RecoveryStarts(NOW + 120, NOW + 30))
            local = gate.queue_read_from(inputs, limits, policy=gate.DEFAULT_ADMISSION_POLICY).explanation
            parsed = wire.parse_reply(wire.encode_reply(wire.ExplainReply(local)))
            assert isinstance(parsed, wire.ExplainReply)
            self.assertEqual(parsed.explanation, local)
            self.assertEqual(parsed.explanation.decided_from, inputs)
            self.assertIsInstance(gate.smallest_head_release(parsed.explanation), gate.EstimateExpired)
            zone = ZoneInfo("UTC")
            self.assertEqual(build_why.release_text(parsed.explanation, {}, zone),
                             build_why.release_text(local, {}, zone))
            self.assertTrue(build_why.release_text(parsed.explanation, {}, zone).startswith("the estimate expired at"))
            assert isinstance(local.head, gate.HeadExplanation)
            assert isinstance(local.head.waiter.reason, gate.EstimateExpired)
            assert isinstance(parsed.explanation.head, gate.HeadExplanation)
            assert isinstance(parsed.explanation.head.waiter.reason, gate.EstimateExpired)
            self.assertEqual(build_why.estimate_refresh_text(parsed.explanation.head.waiter.reason, zone),
                             build_why.estimate_refresh_text(local.head.waiter.reason, zone))
