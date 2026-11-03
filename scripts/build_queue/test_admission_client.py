"""The client's three failure outcomes keep episode identity and time bounds."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr
from dataclasses import replace
import io
import os
import sys
import unittest
from unittest import mock

from scripts.lint import memory_admit as gate
from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue.admission_test_support import (
    AdmissionFixture, Exchange, GIB, ScriptedTransport, TimedChunk, reply_bytes,
)

WAIT = wire.Wait(gate.NeedsMemory(), 1, "head", "free memory")
EXPIRED = wire.Wait(gate.EstimateExpired(1), 1, "head", "estimate expired")
GRANTED = wire.Admitted("stem", GIB, "new-source", "stem.max", "stem.wave")


class ClientTests(unittest.TestCase):
    def use_transport(self, fixture: AdmissionFixture, *exchanges: Exchange) -> ScriptedTransport:
        transport = ScriptedTransport(fixture.clock, *exchanges)
        _ = self.enterContext(mock.patch.object(client, "CLOCK", fixture.clock))
        _ = self.enterContext(mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket))
        return transport

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
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(client.admit(request, lambda _wait: None, lambda: request), GRANTED)
            self.assertEqual(transport.attempts, [0, 3, 7, 11])
            self.assertEqual(output.getvalue().count("admission service unavailable:"), 2)

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
                    peak=gate.ExpectedPeak(2 * GIB, "measured", 9)), fresh_test=True)
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
            lambda: client.release("stem", 137), lambda: client.width("stem", 3),
            lambda: client.admit_head("urgent", "director"))
        for mutation in mutations:
            with self.subTest(mutation=mutation), AdmissionFixture() as fixture:
                transport = self.use_transport(fixture, Exchange(b""), Exchange(reply_bytes(wire.Ok())))
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(mutation(), wire.Ok())
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(transport.requests[0], transport.requests[1])
                self.doCleanups()
