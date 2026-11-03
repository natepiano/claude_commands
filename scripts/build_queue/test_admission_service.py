"""In-process service decisions, persistence and owner events."""
from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stderr
import io
import math
import errno
import os
from pathlib import Path
import stat
import select
import signal
import socket
import sys
import tempfile
import unittest
from unittest import mock

from scripts.lint.memory_test_support import write_config_fixture
from scripts.lint import memory_admit as gate
from scripts.lib.atomic_file import write_text_atomic
from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue import queue_view
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, InjectedCrash, only_admitted


class ServiceTests(unittest.TestCase):
    def test_socket_fixture_works_with_a_long_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="admit-long-", dir="/tmp") as root:
            long_tmpdir = Path(root) / ("nested-" * 24)
            long_tmpdir.mkdir()
            with mock.patch.dict(os.environ, {"TMPDIR": str(long_tmpdir)}), \
                 mock.patch.object(tempfile, "tempdir", str(long_tmpdir)), \
                 AdmissionFixture() as fixture:
                self.assertEqual(fixture.root.parent, long_tmpdir)
                self.assertGreater(len(os.fsencode(fixture.root / "admission.sock")), 108)
                self.assertLess(len(os.fsencode(fixture.socket_path)), 108)
                self.assertTrue(fixture.socket_path.is_socket())
                peer = fixture.connect(wire.ViewRequest())
                self.assertTrue(any(isinstance(reply, wire.ViewReply) for reply in peer.replies()))

    def test_restored_override_grace_starts_at_listener_readiness_once_for_both_sockets(self) -> None:
        for inherited in (False, True):
            with self.subTest(inherited=inherited), AdmissionFixture() as fixture:
                fixture.configure(override_reconnect_seconds=4, reconnect_seconds=2)
                fixture.free(0)
                with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(10)):
                    _ = fixture.server.handle_request(fixture.request())
                    _ = fixture.server.handle_request(wire.AdmitHeadRequest("delayed-head", "urgent", "director"))
                    override = fixture.server.override
                    self.assertNotIsInstance(override, gate.NoHeadAdmissionOverride)
                    fixture.server.close()
                    owner_start = fixture.proc.owner_start

                    def slow_restore(pid: int) -> str | None:
                        fixture.clock.advance(10)
                        return owner_start(pid)

                    with mock.patch.object(fixture.proc, "owner_start", side_effect=slow_restore):
                        fixture.server = fixture.new_server(start=False)
                    self.assertGreater(fixture.clock.monotonic(), 4)
                    self.assertEqual(fixture.server.override, override)
                    self.assertIsInstance(fixture.server.override_reconnect_until, service.ReconnectAwaitingListener)
                    fixture.clock.advance(10)
                    _ = fixture.server.run_pass()
                    self.assertEqual(fixture.server.override, override)
                    # Read both live settings at readiness, rather than restore.
                    fixture.configure(override_reconnect_seconds=1, reconnect_seconds=6)
                    ready_at = fixture.clock.monotonic()
                    if inherited:
                        socket_type = socket.socket
                        with socket_type(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                            listener.bind(str(fixture.socket_path))
                            listener.listen()

                            def inherited_socket(*args: object, **kwargs: object) -> socket.socket:
                                self.assertEqual(args, ())
                                self.assertEqual(kwargs, {"fileno": 3})
                                return socket_type(fileno=os.dup(listener.fileno()))

                            with mock.patch.dict(os.environ, {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid())}), \
                                    mock.patch.object(socket, "socket", side_effect=inherited_socket):
                                fixture.server.start()
                    else:
                        fixture.server.start()
                    self.assertEqual(fixture.server.override_reconnect_until, service.ReconnectDeadline(ready_at + 6))
                    self.assertEqual(fixture.server.next_pass, ready_at + 6)
                    fixture.clock.advance(5.9)
                    fixture.server.start()
                    self.assertEqual(fixture.server.override_reconnect_until, service.ReconnectDeadline(ready_at + 6))
                    fixture.server.run_once(0)
                    self.assertEqual(fixture.server.override, override)
                    fixture.clock.advance(0.1)
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)
                    self.assertIsInstance(wire.parse_record(fixture.state_text(), service.ServiceState).override,
                                          gate.NoHeadAdmissionOverride)

    def test_pressure_held_launch_reconnect_resolves_override_after_promotion_once(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(override_reconnect_seconds=4)
            request = fixture.launch()
            with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(10)):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("launch-head", "urgent", "director"))
                override = fixture.server.override
                self.assertNotIsInstance(override, gate.NoHeadAdmissionOverride)
                fixture.restart()
                fixture.clock.advance(3.9)
                admission_pass = gate.admission_pass

                def pass_crossing_deadline(inputs: gate.QueueInputs, now: float,
                                           limits: gate.AdmissionLimits,
                                           policy: gate.AdmissionPolicy) -> gate.PassResult:
                    self.assertEqual(inputs.override, override)
                    self.assertEqual(len(inputs.ordered), 1)
                    fixture.clock.advance(1)
                    return admission_pass(inputs, now, limits, policy)

                with mock.patch.object(gate, "admission_pass", side_effect=pass_crossing_deadline) as passed:
                    self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                self.assertEqual(passed.call_count, 1)
                self.assertEqual(fixture.server.override, override)
                self.assertIsInstance(fixture.server.override_reconnect_until, service.NoReconnectGrace)
            _ = fixture.server.run_pass()
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.LaunchAdmitted)

    def test_queued_replay_is_read_before_expiring_restored_override(self) -> None:
        for already_accepted in (False, True):
            with self.subTest(already_accepted=already_accepted), AdmissionFixture() as fixture:
                fixture.configure(override_reconnect_seconds=4)
                fixture.free(0)
                request = fixture.request()
                with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(10)):
                    _ = fixture.server.handle_request(request)
                    _ = fixture.server.handle_request(wire.AdmitHeadRequest("queued-head", "urgent", "director"))
                    override = fixture.server.override
                    self.assertNotIsInstance(override, gate.NoHeadAdmissionOverride)
                    fixture.restart()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                        peer.connect(str(fixture.socket_path))
                        if already_accepted:
                            fixture.server.run_once(0)
                            self.assertEqual(len(fixture.server.connections), 1)
                        peer.sendall(wire.encode_request(request).encode())
                        fixture.clock.advance(4)
                        fixture.server.run_once(0)
                        self.assertIn(request.episode, fixture.server.waiters)
                        self.assertEqual(fixture.server.override, override)
                        self.assertIsInstance(fixture.server.override_reconnect_until, service.NoReconnectGrace)

    def test_deadline_drains_large_replays_but_expires_for_incomplete_or_unrelated_traffic(self) -> None:
        for traffic in ("large-new", "large-existing", "incomplete", "unrelated"):
            with self.subTest(traffic=traffic), AdmissionFixture() as fixture:
                fixture.configure(override_reconnect_seconds=4)
                fixture.free(0)
                request = fixture.request()
                request = replace(request, argv=(*request.argv, "x" * 70000))
                with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(10)):
                    _ = fixture.server.handle_request(request)
                    _ = fixture.server.handle_request(wire.AdmitHeadRequest("large-head", "urgent", "director"))
                    override = fixture.server.override
                    self.assertNotIsInstance(override, gate.NoHeadAdmissionOverride)
                    fixture.restart()
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                        peer.connect(str(fixture.socket_path))
                        if traffic == "large-existing":
                            fixture.server.run_once(0)
                            self.assertEqual(len(fixture.server.connections), 1)
                        encoded = wire.encode_request(request).encode()
                        self.assertGreater(len(encoded), 65536)
                        if traffic == "incomplete":
                            encoded = encoded[:-1]
                        elif traffic == "unrelated":
                            encoded = wire.encode_request(wire.ViewRequest()).encode()
                        peer.sendall(encoded)
                        fixture.clock.advance(4)
                        fixture.server.run_once(0)
                        if traffic.startswith("large-"):
                            self.assertIn(request.episode, fixture.server.waiters)
                            self.assertEqual(fixture.server.override, override)
                        else:
                            self.assertNotIn(request.episode, fixture.server.waiters)
                            self.assertIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)
                        self.assertIsInstance(fixture.server.override_reconnect_until, service.NoReconnectGrace)

    def test_second_request_and_oversized_input_end_admission_before_next_pass(self) -> None:
        for violation in ("second-request", "oversize-newline", "oversize-incomplete"):
            with self.subTest(violation=violation), AdmissionFixture() as fixture:
                fixture.free(0)
                request = fixture.request()
                if violation == "second-request":
                    peer = fixture.connect(request)
                    self.assertIsInstance(peer.replies()[0], wire.Wait)
                    connection = next(iter(fixture.server.connections.values()))
                    original_wait = fixture.server.waiters[request.episode].last_reply
                    assert isinstance(original_wait, wire.Wait)
                    peer.socket.sendall(wire.encode_request(wire.ViewRequest()).encode())
                else:
                    encoded = wire.encode_request(request)
                    if violation == "oversize-incomplete":
                        encoded = encoded[:-1]
                    fixture.configure(request_max_bytes=len(encoded.encode()) - 1)
                    peer = fixture.connect(encoded)
                    # Accept and read it during the next receive phase.
                    connection = None
                    original_wait = None
                # A due pass can admit the step as soon as reception completes.
                fixture.free(60 * GIB)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertNotIn(request.episode, fixture.server.waiters)
                self.assertNotIn(request.episode, fixture.server.launches)
                self.assertNotIn(request.episode, fixture.server.reservations)
                self.assertNotIn(request.episode, fixture.server.ledger)
                if connection is None:
                    connection = next(iter(fixture.server.connections.values()))
                self.assertTrue(connection.finished)
                if original_wait is not None:
                    sending = connection.sending
                    fixture.server.send_wait(connection, original_wait)
                    self.assertTrue(connection.finished)
                    self.assertEqual(connection.sending, sending)
                replies = peer.replies()
                self.assertEqual(len(replies), 1)
                refused = replies[0]
                self.assertIsInstance(refused, wire.Refused)
                assert isinstance(refused, wire.Refused)
                self.assertIn("one request" if violation == "second-request" else "exceeds", refused.reason)
                self.assertEqual(fixture.server.connections, {})
                self.assertEqual(peer.socket.recv(1), b"")
                with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                    for _ in range(3):
                        self.assertEqual(fixture.server.epoll.poll(0), [])
                        fixture.server.run_once(0)
                    self.assertEqual(passed.call_count, 0)

    def test_scope_watch_closes_on_release_and_reused_scope_is_watched_again(self) -> None:
        with AdmissionFixture() as fixture:
            events = fixture.scope_events()
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            descriptor = next(fd for fd, watch in fixture.server.watchers.items()
                              if isinstance(watch, service.ScopeWatch))
            watch = fixture.server.watchers[descriptor]
            assert isinstance(watch, service.ScopeWatch)
            self.assertEqual(watch.episode, request.episode)
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-scope", grant.reservation, 0))
            self.assertIn(descriptor, events.unregistered)
            with self.assertRaises(OSError):
                _ = os.fstat(descriptor)
            fixture.clock.advance(0.001)
            second = replace(request, episode="scope-reused", sidecar=str(fixture.scope(request.owner)))
            _ = only_admitted(fixture.server.handle_request(second))
            _ = fixture.server.run_pass()
            self.assertEqual(len(events.registered), 2)
            scoped = [watch for watch in fixture.server.watchers.values() if isinstance(watch, service.ScopeWatch)]
            self.assertEqual(scoped, [service.ScopeWatch(watch.path, second.episode)])

    def test_removed_scope_wakeup_closes_watch_and_continues_the_loop(self) -> None:
        for error in (errno.ENODEV, errno.ENOENT):
            with self.subTest(error=error), AdmissionFixture() as fixture:
                events = fixture.scope_events()
                request = fixture.request()
                sidecar = fixture.scope(request.owner)
                request = replace(request, sidecar=str(sidecar))
                _ = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
                descriptor = next(fd for fd, watch in fixture.server.watchers.items()
                                  if isinstance(watch, service.ScopeWatch))
                scope = Path(sidecar.read_text())
                (scope / "cgroup.procs").unlink()
                (scope / "cgroup.events").unlink()
                original_read = os.read
                def removed_read(fd: int, size: int) -> bytes:
                    if fd == descriptor:
                        raise OSError(error, "scope removed")
                    return original_read(fd, size)
                events.events.append((descriptor, select.EPOLLERR))
                fixture.readings.counts.clear()
                with mock.patch.object(os, "read", side_effect=removed_read):
                    fixture.server.run_once(0)
                self.assertIn(descriptor, events.unregistered)
                self.assertNotIn(descriptor, fixture.server.watchers)
                self.assertEqual(fixture.readings.counts["host"], 1)
                self.assertFalse(fixture.server.stopped)

    def test_zero_wait_socket_restart_recovers_after_sigkill_and_rejects_a_live_listener(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.kill_socket_server()
            self.assertTrue(fixture.socket_path.exists())
            fixture.server = fixture.new_server()
            peer = fixture.connect(wire.ViewRequest())
            self.assertIsInstance(peer.replies()[0], wire.ViewReply)
            contender = service.AdmissionServer(fixture.state_dir, fixture.clock, fixture.readings,
                fixture.proc, socket_path=fixture.socket_path)
            try:
                with self.assertRaisesRegex(RuntimeError, "already listening"):
                    contender.start()
                self.assertTrue(fixture.socket_path.exists())
            finally:
                contender.close()

    def test_socket_permissions_are_private_at_bind_before_listen(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            bind = socket.socket.bind
            modes: list[int] = []
            def observe_bind(listener: socket.socket, address: str) -> None:
                bind(listener, address)
                modes.append(stat.S_IMODE(Path(address).stat().st_mode))
            previous = os.umask(0)
            try:
                with mock.patch.object(socket.socket, "bind", observe_bind):
                    fixture.server = fixture.new_server()
            finally:
                _ = os.umask(previous)
            self.assertEqual(modes, [0o600])

    def test_pressure_held_override_survives_restart_until_its_waiter_reconnects(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(override_reconnect_seconds=4)
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=12 * GIB)))
            fixture.free(16 * GIB)
            request = fixture.request(need=12 * GIB)
            mutation = wire.AdmitHeadRequest("persisted-head", "urgent", "director")
            with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(10)):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                override = fixture.server.override
                self.assertNotIsInstance(override, gate.NoHeadAdmissionOverride)
                fixture.restart()
                self.assertEqual(fixture.server.override, override)
                fixture.clock.advance(3)
                fixture.server.run_once(0)
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                self.assertEqual(fixture.server.override, override)
                _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.server.override, override)
                saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                self.assertEqual(saved.override, override)
            _ = fixture.server.run_pass()
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.Admitted)

    def test_restored_override_lapses_after_configured_reconnect_chance(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(override_reconnect_seconds=4)
            fixture.free(0)
            with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(10)):
                _ = fixture.server.handle_request(fixture.request())
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("head-grace", "urgent", "director"))
                fixture.restart()
                fixture.clock.advance(3.9)
                fixture.server.run_once(0)
                self.assertNotIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)
                fixture.clock.advance(0.1)
                fixture.server.run_once(0)
                self.assertIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)
                self.assertIsInstance(wire.parse_record(fixture.state_text(), service.ServiceState).override,
                                      gate.NoHeadAdmissionOverride)

    def test_mutation_refusals_replay_durably_after_state_changes(self) -> None:
        with AdmissionFixture() as fixture:
            empty = wire.AdmitHeadRequest("refused-head", "urgent", "director")
            refused = fixture.server.handle_request(empty)
            fixture.restart()
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request())
            self.assertEqual(fixture.server.handle_request(empty), refused)
            self.assertIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)
            fixture.free(60 * GIB)
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            width = wire.WidthRequest("refused-width", grant.reservation, 0)
            refused_width = fixture.server.handle_request(width)
            self.assertIsInstance(refused_width[0], wire.Refused)
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(replace(width, width=2)), refused_width)
            self.assertEqual(fixture.server.reservations[request.episode].test_width, request.test_width)

    def test_width_replays_live_through_retention_then_expire_after_release(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(replay_retention_seconds=2)
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            old = wire.WidthRequest("old-width", grant.reservation, 4)
            _ = fixture.server.handle_request(old)
            _ = fixture.server.handle_request(replace(old, request_id="new-width", width=2))
            fixture.clock.advance(3)
            _ = fixture.server.run_pass()
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(old), [wire.Ok()])
            self.assertEqual(fixture.server.reservations[request.episode].test_width, wire.RequestedTestWidth(2))
            _ = fixture.server.handle_request(wire.ReleaseRequest("end-width", grant.reservation, 0))
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            self.assertIn(old.request_id, fixture.server.ledger)
            fixture.clock.advance(2)
            _ = fixture.server.run_pass()
            self.assertNotIn(old.request_id, fixture.server.ledger)

    def test_admission_and_width_use_the_shared_need_reader_with_private_history(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            need = gate.TestPhaseNeed(2 * GIB, 7, 8, 8, "same_width", margin=1.7)
            with mock.patch.object(gate, "test_phase_need", return_value=need) as reader:
                grant = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(fixture.server.reservations[request.episode].test_phase_need, need)
                changed = replace(need, width=4, bytes=GIB)
                reader.return_value = changed
                _ = fixture.server.handle_request(wire.WidthRequest("shared-reader", grant.reservation, 4))
                self.assertEqual(fixture.server.reservations[request.episode].test_phase_need, changed)
            self.assertEqual(reader.call_count, 2)
            for call in reader.call_args_list:
                self.assertEqual(call.kwargs["history_path"], fixture.state_dir / "anon_peaks.jsonl")

    def test_promoted_launch_and_input_composition_share_waiter_ordering(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            launch = fixture.launch(since=fixture.clock.now() - 10)
            _ = fixture.server.handle_request(launch)
            with fixture.server.observe_waiter_ordering() as ordered:
                later = fixture.request()
                _ = fixture.server.handle_request(later)
            self.assertGreaterEqual(ordered.call_count, 2)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered], [launch.owner.pid, later.owner.pid])

    def test_launch_hold_reason_comes_from_the_shared_reason_rule(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            reason = gate.EstimateExpired(fixture.clock.now())
            with mock.patch.object(gate, "queue_wait_reason", return_value=reason) as explain:
                reply = fixture.server.handle_request(fixture.launch())[0]
            assert isinstance(reply, wire.Wait)
            self.assertEqual(reply.reason, reason)
            self.assertEqual(reply.text, queue_view.reason_phrase(reason, ()))
            self.assertEqual(explain.call_count, 1)

    def test_unchanged_passes_do_not_rewrite_durable_state(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            _ = fixture.server.handle_request(fixture.request())
            state = fixture.state_text()
            with mock.patch.object(service, "write_text_atomic", wraps=write_text_atomic) as write:
                for _ in range(3):
                    fixture.clock.advance(1)
                    _ = fixture.server.run_pass()
                self.assertEqual(write.call_count, 0)
                self.assertEqual(fixture.state_text(), state)
                _ = fixture.server.handle_request(fixture.request())
                self.assertGreater(write.call_count, 0)

    def test_delivered_waits_leave_only_the_current_reply_and_no_pending_history(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request()
            first = fixture.server.handle_request(request)
            self.assertIsInstance(first[0], wire.Wait)
            for available in (GIB, 2 * GIB, 0):
                fixture.free(available)
                fixture.clock.advance(60)
                _ = fixture.server.run_pass()
                self.assertIsInstance(fixture.server.waiters[request.episode].last_reply, wire.Wait)
                self.assertEqual(fixture.server.pending_replies, {})
                self.assertNotIn(request.episode, fixture.server.ledger)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            self.assertNotIn(request.episode, fixture.server.waiters)
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.Admitted)
            self.assertEqual(fixture.server.pending_replies, {})

    def test_wait_and_source_lifetimes_use_distinct_states(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            with fixture.server.observe_wait_initial_states() as before_delivery:
                request = fixture.request()
                _ = fixture.server.handle_request(request)
            self.assertEqual(before_delivery, [service.NoWaitReply()])
            self.assertIsInstance(fixture.server.waiters[request.episode].last_reply, wire.Wait)
            self.assertIsInstance(fixture.server.source_change_at, service.SourcesUnchanged)
            source = fixture.root / "removed-source.py"
            _ = source.write_text("loaded")
            info = source.stat()
            fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
            source.unlink()
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            self.assertIsInstance(fixture.server.sources[source], service.SourceMissing)
            self.assertEqual(fixture.server.source_change_at, fixture.clock.monotonic())
            self.assertTrue(all(isinstance(watch, service.OwnerWatch) for watch in fixture.server.watchers.values()))

    def test_wait_poll_and_heartbeat_intervals_follow_config_changes(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(waiting_pass_seconds=3, wait_reply_seconds=7)
            fixture.free(0)
            peer = fixture.connect(fixture.request())
            self.assertEqual(len(peer.replies()), 1)
            fixture.readings.counts.clear()
            fixture.clock.advance(2)
            self.assertFalse(peer.replies())
            self.assertEqual(fixture.readings.counts["host"], 0)
            fixture.clock.advance(1)
            self.assertFalse(peer.replies())
            self.assertEqual(fixture.readings.counts["host"], 1)
            fixture.configure(waiting_pass_seconds=1, wait_reply_seconds=4)
            fixture.clock.advance(3)
            self.assertEqual(len(peer.replies()), 1)
            self.assertEqual(fixture.server.next_pass, fixture.clock.monotonic() + 1)

    def test_source_check_and_quiet_window_follow_config(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(source_check_seconds=2, source_settle_seconds=4)
            fixture.server.next_source_check = fixture.clock.monotonic() + 2
            source = fixture.root / "loaded-config.py"
            _ = source.write_text("old")
            info = source.stat()
            fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
            source.unlink()
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            self.assertIsInstance(fixture.server.source_change_at, service.SourcesUnchanged)
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            fixture.clock.advance(2)
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.stopped)
            fixture.clock.advance(2)
            fixture.server.run_once(0)
            self.assertTrue(fixture.server.stopped)

    def test_serve_binds_the_environment_socket_without_listen_fds(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            socket_path = fixture.socket_dir / "from-environment.sock"
            run_once = service.AdmissionServer.run_once
            def one_turn(server: service.AdmissionServer) -> None:
                run_once(server, 0)
                self.assertEqual(server.socket_path, socket_path)
                self.assertTrue(socket_path.is_socket())
                self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
            with mock.patch.dict(os.environ, {"ADMISSION_SOCKET": str(socket_path), "LISTEN_FDS": ""}), \
                 mock.patch.object(service, "SystemClock", return_value=fixture.clock), \
                 mock.patch.object(service, "SystemReadings", return_value=fixture.readings), \
                 mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(service.AdmissionServer, "serve_forever", one_turn), \
                 mock.patch.object(sys, "argv", ["admission_service", "serve", "--state-dir", str(fixture.state_dir)]):
                self.assertEqual(service.main(), 0)
            self.assertFalse(socket_path.exists())

    def test_each_pass_matches_the_shared_rule_and_view_uses_its_frozen_limits(self) -> None:
        for free in (0, 10 * GIB, 60 * GIB):
            with self.subTest(free=free), AdmissionFixture() as fixture:
                fixture.free(0)
                requests = [fixture.request(need=need * GIB, since=fixture.clock.now() - age)
                            for need, age in ((12, 400), (4, 30), (2, 10))]
                for request in requests:
                    _ = fixture.server.handle_request(request)
                fixture.free(free)
                with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as run:
                    result = fixture.server.run_pass()
                self.assertEqual(run.call_count, 1)
                inputs, limits = fixture.server.last_inputs, fixture.server.last_limits
                assert isinstance(inputs, gate.QueueInputs)
                assert isinstance(limits, gate.AdmissionLimits)
                self.assertEqual(result, gate.admission_pass(inputs, inputs.now, limits, policy=gate.DEFAULT_ADMISSION_POLICY))
                starts = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                    key=lambda row: row.admission.rank if isinstance(row.admission, gate.ProposedAdmission) else -1)
                committed = list(fixture.server.reservations.values())
                self.assertEqual([f"{record.owner.pid}-{record.owner.start_time}" for record in committed],
                                 [row.owner for row in starts])
                for row in result.rows:
                    request = next(request for request in requests if gate.waiter_owner({
                        "pid": request.owner.pid, "start_time": request.owner.start_time,
                        "since": request.since, "need": request.profile.peak.bytes}) == row.owner)
                    if isinstance(row.admission, gate.NotAdmitted):
                        waiting = fixture.server.waiters[request.episode].last_reply
                        assert isinstance(waiting, wire.Wait)
                        self.assertEqual(waiting.reason, row.reason)
                        self.assertEqual(waiting.text, queue_view.reason_phrase(row.reason,
                            gate.queue_read_from(inputs, limits, policy=gate.DEFAULT_ADMISSION_POLICY).view.waiting))
                expected = gate.queue_read_from(inputs, limits, policy=gate.DEFAULT_ADMISSION_POLICY)
                with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "1"}):
                    self.assertEqual(fixture.server.handle_request(wire.ViewRequest()), [wire.ViewReply(expected)])
                    explained = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(explained, wire.ExplainReply)
                self.assertEqual(explained.explanation.decided_from, inputs)
                self.assertEqual(explained.explanation.limits, limits)

    def test_wait_lines_then_ranked_admissions_and_release_frees_charge(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            peers = [fixture.connect(fixture.request(need=8 * GIB)) for _ in range(2)]
            for peer in peers:
                self.assertTrue(any(isinstance(reply, wire.Wait) for reply in peer.replies()))
            fixture.free(60 * GIB)
            result = fixture.server.run_pass()
            expected = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                              key=lambda row: row.admission.rank if isinstance(row.admission, gate.ProposedAdmission) else -1)
            grants = [only_admitted(peer.replies()) for peer in peers]
            self.assertEqual([record.owner.pid for record in fixture.server.reservations.values()],
                             [int(row.owner.split('-')[0]) for row in expected])
            before = fixture.clock.monotonic()
            _ = fixture.server.handle_request(wire.ReleaseRequest("end-first", grants[0].reservation, 0))
            self.assertLess(fixture.clock.monotonic() - before, 0.2)
            self.assertEqual(len(fixture.server.reservations), 1)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertEqual(len(inputs.reservations), 1)

    def test_width_and_call_states_survive_state_and_legacy_boundary(self) -> None:
        cases: tuple[tuple[wire.TestWidth, wire.Call, str], ...] = (
            (wire.RequestedTestWidth(3), wire.CallRecord("recorded"), "nextest"),
            (wire.UnspecifiedTestWidth(), wire.NoCallRecord(), "nextest"),
            (wire.NoTestWidth(), wire.NoCallRecord(), "clippy"))
        for width, call, step in cases:
            with self.subTest(width=width), AdmissionFixture() as fixture:
                request = fixture.request(width=width, call=call, step=step)
                granted = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                row = service.reservation_row(record)
                self.assertEqual(row["test_threads"], width.threads if isinstance(width, wire.RequestedTestWidth) else None)
                self.assertEqual(row["call_id"], call.call_id if isinstance(call, wire.CallRecord) else None)
                self.assertEqual(service.reservation_record(request.episode, granted.reservation, row), record)
                fixture.restart()
                self.assertEqual(fixture.server.reservations[request.episode], record)
                self.assertNotIn('null', fixture.state_text())
                self.assertEqual(only_admitted(fixture.server.handle_request(request)), granted)
                self.assertEqual(len(fixture.server.reservations), 1)

    def test_running_steps_refresh_without_waiters_and_idle_service_does_not_poll(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "lint.conf"
            _ = write_config_fixture(config, "running_pass_seconds=3\n")
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            granted = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            need = gate.TestPhaseNeed(2 * GIB, 5, 8, 8, "same_width")
            fixture.server.reservations[request.episode] = replace(record, test_phase_need=need)
            fixture.clock.advance(gate.DEFAULT_ADMISSION_POLICY.timing.ramp_seconds)
            row = service.reservation_row(fixture.server.reservations[request.episode])
            phase = gate.test_phase_path(row)
            assert isinstance(phase, Path)
            fixture.scoped_peak(granted, 11 * GIB)
            for peak in (GIB, 5 * GIB):
                # Sampler schema comes from buildlog.record; the reader owns its parsing.
                _ = phase.write_text('{"repo_identity":"fixture","test_scope":"full","package":"",' +
                    '"filters":[],"call_id":"call-1","test_threads":8,' +
                    f'"test_phase_started_at":{fixture.clock.now() - 1},"test_phase_peak_bytes":{peak}' + '}')
                fixture.clock.advance(3)
                fixture.server.run_once(0)
                reply = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(reply, wire.ViewReply)
                self.assertFalse(reply.reading.view.waiting)
                self.assertEqual(reply.reading.explanation.running[0].charged,
                                 math.ceil(max(peak, need.bytes) * need.margin))
            _ = fixture.server.handle_request(wire.ReleaseRequest("end", granted.reservation, 0))
            fixture.readings.counts.clear()
            fixture.clock.advance(100)
            fixture.server.run_once(0)
            self.assertEqual(fixture.readings.counts["host"], 0)

    def test_supplied_readings_are_taken_once_per_pass(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.handle_request(fixture.request())
            fixture.readings.counts.clear()
            _ = fixture.server.run_pass()
            self.assertEqual(set(fixture.readings.counts.values()), {1})
            self.assertEqual(fixture.readings.counts["host"], 1)
            self.assertEqual(fixture.readings.counts["pressure"], 1)
            self.assertEqual(fixture.readings.counts["compilers"], 1)

    def test_crash_after_grant_persistence_replays_one_reservation_and_place(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            fixture.fault.stage = "after_state_write"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(request)
            fixture.restart()
            granted = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(granted, only_admitted(fixture.server.handle_request(request)))
            self.assertEqual(len(fixture.server.reservations), 1)
            self.assertEqual(len(fixture.server.places), 1)
            record = fixture.server.reservations[request.episode]
            self.assertEqual(granted.reservation, record.path)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertGreaterEqual(gate.build_charge(inputs.running[0].measured), request.profile.peak.bytes)

    def test_release_crashes_finish_once_from_saved_intent_or_history(self) -> None:
        for boundary in ("after_state_write", "before_history_append", "after_history_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                granted = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(granted)
                record = fixture.server.reservations[request.episode]
                release = wire.ReleaseRequest("release-crashed", granted.reservation, 137)
                if boundary == "before_history_append":
                    with mock.patch.object(gate.store, "locked_append", side_effect=InjectedCrash(boundary)):
                        with self.assertRaises(InjectedCrash):
                            _ = fixture.server.handle_request(release)
                    self.assertTrue(Path(granted.reservation).exists())
                    self.assertIn("finalization", gate.read_reservation(Path(granted.reservation)))
                    self.assertFalse(fixture.history())
                else:
                    def stop_after_durable_release(stage: str) -> None:
                        if stage != boundary:
                            return
                        saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                        if boundary == "after_state_write" and release.request_id not in saved.ledger:
                            return
                        raise InjectedCrash(boundary)
                    with mock.patch.object(fixture.server, "fault", side_effect=stop_after_durable_release):
                        with self.assertRaises(InjectedCrash):
                            _ = fixture.server.handle_request(release)
                    if boundary == "after_state_write":
                        saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                        self.assertEqual(saved.ledger[release.request_id].reply, wire.Ok())
                        self.assertFalse(saved.reservations)
                        self.assertEqual(len(fixture.history()), 1)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                rows = fixture.history()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], record.reservation_id)
                self.assertEqual(rows[0]["outcome"], "killed")
                self.assertFalse(Path(granted.reservation).exists())
                self.assertFalse(Path(granted.max_path).exists())
                self.assertFalse(fixture.server.reservations)

    def test_mutation_request_ids_replay_without_repeating_changes(self) -> None:
        with AdmissionFixture() as fixture:
            granted = only_admitted(fixture.server.handle_request(fixture.request()))
            width = wire.WidthRequest("width-once", granted.reservation, 4)
            self.assertEqual(fixture.server.handle_request(width), [wire.Ok()])
            state = fixture.state_text()
            self.assertEqual(fixture.server.handle_request(replace(width, width=2)), [wire.Ok()])
            self.assertEqual(fixture.state_text(), state)
            release = wire.ReleaseRequest("release-once", granted.reservation, 0)
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            state = fixture.state_text()
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            self.assertEqual(fixture.state_text(), state)

    def test_restart_thirty_waiters_keeps_places_and_order_fields(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            requests = [fixture.request(since=fixture.clock.now() - 600 + index) for index in range(30)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            places = fixture.server.places.copy()
            fixture.restart()
            for request in reversed(requests):
                _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered], [request.owner.pid for request in requests])
            self.assertEqual(fixture.server.places, places)
            for _, row in inputs.ordered:
                self.assertEqual(gate.waiter_priority(row), gate.NormalOrder())
                self.assertNotIn("head_since", row)
                self.assertNotIn("head_state", row)
            for forbidden in ('"head_since"', '"head_state"', '"bump_until"', '"bump_ends_at"'):
                self.assertNotIn(forbidden, fixture.state_text())

    def test_snapshot_ranking_keeps_priority_then_run_place_then_pid_order(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            joined = fixture.clock.now() - 600
            first = fixture.request(since=joined)
            tied = fixture.request(since=joined)
            younger = fixture.request(since=joined + 1)
            for request in (younger, tied, first):
                _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered],
                             [first.owner.pid, tied.owner.pid, younger.owner.pid])
            rows = {row["pid"]: row for _, row in inputs.ordered}
            self.assertEqual([rows[request.owner.pid]["since"] for request in (first, tied, younger)],
                             [joined, joined, joined + 1])
            priority = gate.rank_waiter(rows[younger.owner.pid], gate.PriorityUnit("urgent"))
            ranked = sorted([rows[tied.owner.pid], priority, rows[first.owner.pid]], key=gate.waiter_order)
            self.assertEqual([row["pid"] for row in ranked],
                             [younger.owner.pid, first.owner.pid, tied.owner.pid])
            self.assertEqual(priority["since"], younger.since)

    def test_two_episodes_of_one_step_reuse_its_first_place(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(since=fixture.clock.now() - 200)
            granted = only_admitted(fixture.server.handle_request(first))
            places = tuple(fixture.server.places.values())
            _ = fixture.server.handle_request(wire.ReleaseRequest("token-release", granted.reservation, 0))
            fixture.free(0)
            second = replace(first, episode="after-token")
            _ = fixture.server.handle_request(second)
            waiting = fixture.server.waiters[second.episode]
            self.assertEqual(waiting.row["since"], first.since)
            place = tuple(fixture.server.places.values())[0]
            self.assertEqual((place.priority_rank, place.priority_since),
                             (places[0].priority_rank, places[0].priority_since))
            fixture.free(60 * GIB)
            fixture.clock.advance(0.001)
            _ = fixture.server.run_pass()
            later = only_admitted([fixture.server.ledger[second.episode].reply])
            self.assertNotEqual(granted.reservation, later.reservation)
            self.assertEqual(set(fixture.server.reservations), {second.episode})

    def test_owner_exit_removes_waiter_and_unscoped_reservation_through_pidfd(self) -> None:
        for waiting in (True, False):
            with self.subTest(waiting=waiting), AdmissionFixture() as fixture:
                fixture.free(0 if waiting else 60 * GIB)
                request = fixture.request()
                _ = fixture.server.handle_request(request)
                fixture.proc.kill(request.owner)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.waiters)
                self.assertFalse(fixture.server.reservations)
                self.assertFalse(fixture.server.places)

    def test_scope_outlives_its_shell_and_empty_scope_event_releases_charge(self) -> None:
        with AdmissionFixture() as fixture:
            owner, scope, stop = fixture.transient_scope()
            sidecar = fixture.root / "real-scope.cgroup"
            _ = sidecar.write_text(str(scope))
            request = replace(fixture.request(owner=owner), sidecar=str(sidecar))
            guard = gate.check_memory_reading
            def membership_only(path: Path) -> None:
                # cgroup.procs is lifecycle evidence; no live memory file is allowed.
                if path != scope / "cgroup.procs":
                    guard(path)
            def supplied_scope_memory(_path: Path, row: gate.Reservation, _now: object,
                                      _processes: object, _proc: Path, policy: gate.AdmissionPolicy) -> gate.StepMemory:
                _ = policy
                lifetime = fixture.proc.lifetime(row, fixture.proc.owner_start(owner.pid) == owner.start_time)
                return gate.GoneMemory() if isinstance(lifetime, gate.EndedStep) else gate.MeasuredMemory(GIB)
            with mock.patch.object(gate, "check_memory_reading", side_effect=membership_only), \
                 mock.patch.object(fixture.readings, "reservation", side_effect=supplied_scope_memory):
                _ = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
                descriptor = os.pidfd_open(owner.pid)
                try:
                    os.kill(owner.pid, signal.SIGKILL)
                    self.assertTrue(select.select([descriptor], [], [], 3)[0], "owner must exit")
                finally:
                    os.close(descriptor)
                fixture.server.run_once(0)
                self.assertEqual(len(fixture.server.reservations), 1)
                stop()
                fixture.server.run_once(0)
                # Ended lifetime comes from the real empty scope, not owner polling.
                self.assertFalse(fixture.server.reservations)

    def test_closed_waiting_connection_leaves_queue_but_grant_survives_disconnect(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            peer = fixture.connect(fixture.request())
            _ = peer.replies()
            peer.close()
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.waiters)
            fixture.free(60 * GIB)
            peer = fixture.connect(fixture.request())
            _ = only_admitted(peer.replies())
            peer.close()
            fixture.server.run_once(0)
            self.assertEqual(len(fixture.server.reservations), 1)

    def test_version_two_client_gets_explicit_refusal_and_socket_is_private(self) -> None:
        with AdmissionFixture() as fixture:
            peer = fixture.connect('{"v":2,"type":"view","data":{}}\n')
            self.assertEqual(peer.replies(), [wire.Refused("client version 2, service supports 1..1")])
            self.assertEqual(stat.S_IMODE(fixture.socket_path.stat().st_mode), 0o600)

    def test_aged_launch_alone_still_checks_own_need_compiler_memory_and_free_memory(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(4 * GIB)
            fixture.compiler(8 * GIB)
            request = fixture.launch(since=fixture.clock.now() - gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds - 1)
            replies = fixture.server.handle_request(request)
            self.assertIsInstance(replies[0], wire.Wait)
            expected = gate.decide(4 * GIB, 60 * GIB, 8 * GIB, [gate.CompilerServiceMemory(8 * GIB)])
            waiting = replies[0]
            assert isinstance(waiting, wire.Wait)
            self.assertEqual(waiting.reason, gate.queue_wait_reason(expected, request.profile.peak.bytes,
                4 * GIB, gate.RecordedQueueIdentities()))
            self.assertEqual(waiting.text, queue_view.reason_phrase(waiting.reason, ()))
            self.assertEqual((expected.state, expected.free_passes, expected.measured, expected.head_room),
                             ("hold", False, 8 * GIB, gate.NoHeadRoom()))
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            view = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(view, wire.ViewReply)
            self.assertFalse(view.reading.view.waiting)
            fixture.free(60 * GIB)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.LaunchAdmitted)
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(wire.parse_record(fixture.state_text(), service.ServiceState).reservations, {})

    def test_expiry_and_refresh_keep_episode_place_and_order_fields(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request(age=gate.DEFAULT_ADMISSION_POLICY.timing.head_seconds - 1)
            young = fixture.server.handle_request(request)[0]
            assert isinstance(young, wire.Wait)
            self.assertNotIsInstance(young.reason, gate.EstimateExpired)
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            expired = fixture.server.waiters[request.episode].last_reply
            assert isinstance(expired, wire.Wait)
            self.assertIsInstance(expired.reason, gate.EstimateExpired)
            place = tuple(fixture.server.places.values())[0]
            refreshed = replace(request, profile=replace(request.profile, estimated_at=fixture.clock.now()))
            _ = fixture.server.handle_request(refreshed)
            self.assertEqual(tuple(fixture.server.places.values()), (place,))
            self.assertEqual(len(fixture.server.waiters), 1)

    def test_admit_head_records_actor_once_and_changes_the_pass(self) -> None:
        with AdmissionFixture() as fixture:
            mutation = wire.AdmitHeadRequest("override", "urgent", "director")
            self.assertEqual(fixture.server.handle_request(replace(mutation, request_id="empty-head")), [wire.Refused("nothing waits; no head to admit")])
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=12 * GIB)))
            fixture.free(16 * GIB)
            request = fixture.request(need=12 * GIB)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            with mock.patch.object(gate, "head_override_event") as event:
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                _ = fixture.server.handle_request(mutation)
            self.assertEqual(event.call_args_list[0].args[0], "set")
            self.assertEqual(event.call_args_list[0].args[1]["set_by"], "director")
            self.assertEqual(sum(call.args[0] == "set" for call in event.call_args_list), 1)
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.Admitted)
            self.assertNotIn(request.episode, fixture.server.waiters)

    def test_loaded_source_change_or_deletion_stops_after_two_quiet_seconds(self) -> None:
        for delete in (False, True):
            with self.subTest(delete=delete), AdmissionFixture() as fixture:
                source = fixture.root / "loaded.py"
                _ = source.write_text("old")
                info = source.stat()
                fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
                if delete:
                    source.unlink()
                else:
                    _ = source.write_text("new code")
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.stopped)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.stopped)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertTrue(fixture.server.stopped)
                self.assertTrue((fixture.state_dir / "state.json").exists())

    def test_serve_requires_explicit_state_dir(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["admission_service", "serve"]), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                _ = service.main()
            self.assertEqual(error.exception.code, 2)
        self.assertIn("the following arguments are required: --state-dir", stderr.getvalue())
