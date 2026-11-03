"""In-process service decisions, persistence and owner events."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
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
from typing import cast
from unittest import mock

from scripts.lint.memory_test_support import write_config_fixture
from scripts.lint import memory_admit as gate
from scripts.lib import test_hang_guard
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.message.roster import UnitLabels
from scripts.lint.build_shape import build_shape
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service as service
from scripts.build_queue import queue_snapshot, queue_view
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, InjectedCrash, only_admitted


class ServiceTests(unittest.TestCase):
    def test_each_pass_publishes_captured_readings_and_remaining_waiters(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            self.assertIsInstance(snapshot, queue_snapshot.QueueSnapshot)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertEqual(snapshot.inputs.host, inputs.host)
            self.assertEqual(snapshot.inputs.pressure, inputs.pressure)
            self.assertEqual(snapshot.inputs.running, inputs.running)
            self.assertEqual(snapshot.inputs.reservations, inputs.reservations)
            self.assertEqual([row for _, row in snapshot.inputs.ordered],
                             [{**waiting.row, "order_since": waiting.row["since"]}
                              for waiting in fixture.server.waiters.values()])
            self.assertEqual(snapshot.written_at, fixture.clock.now())
            self.assertEqual(snapshot.policy, fixture.server.last_policy)
            fixture.clock.advance(1)
            fixture.free(60 * GIB)
            counts = fixture.readings.counts.copy()
            _ = fixture.server.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.written_at, fixture.clock.now())
            self.assertEqual(snapshot.inputs.ordered, [])
            self.assertIn(request.episode, fixture.server.reservations)
            self.assertEqual(fixture.readings.counts["host"] - counts["host"], 1)
            self.assertEqual(fixture.readings.counts["pressure"] - counts["pressure"], 1)
            self.assertEqual(fixture.readings.counts["compilers"] - counts["compilers"], 1)

    def test_snapshot_replacement_failure_keeps_the_last_complete_snapshot(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.run_pass()
            previous = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            with mock.patch.object(os, "replace", side_effect=OSError("snapshot unavailable")):
                with self.assertRaises(OSError):
                    queue_snapshot.write_queue_snapshot(fixture.state_dir, replace(inputs, now=inputs.now + 1),
                                                        fixture.server.last_policy)
            self.assertEqual(queue_snapshot.read_queue_snapshot(fixture.state_dir), previous)
            self.assertFalse((fixture.state_dir / "queue.json.tmp").exists())
            with mock.patch.object(queue_snapshot, "write_queue_snapshot", side_effect=OSError("snapshot unavailable")):
                _ = fixture.server.run_pass()
            self.assertEqual(queue_snapshot.read_queue_snapshot(fixture.state_dir), previous)

    def test_snapshot_accepts_unowned_memory_without_a_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            unowned = fixture.root / "builds.slice/unowned.scope"
            unowned.mkdir(parents=True)
            _ = (unowned / "memory.current").write_text(str(GIB))
            _ = (unowned / "cgroup.procs").write_text("")
            _ = fixture.server.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs, fixture.server.last_inputs)
            self.assertTrue(any(isinstance(step.measured, gate.UnownedSliceMemory) for step in snapshot.inputs.running))

    def test_unreadable_owner_refuses_without_waiter_or_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            with mock.patch.object(fixture.proc, "owner_start", return_value=service.OwnerProcessGone()):
                self.assertEqual(fixture.server.handle_request(request), [wire.Refused("owner no longer exists")])
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)

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

                    def slow_restore(pid: int) -> service.LiveOwnerIdentity | service.OwnerProcessGone:
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
                    self.assertEqual(fixture.server.next_pass, min(ready_at + 6,
                        ready_at + gate.read_memory_config().running_pass_seconds))
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
            self.assertEqual(fixture.server.reservations[request.episode].test_width, gate.RequestedTestWidth(2))
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
                fixture.server.resolve_estimates()
                self.assertEqual(fixture.server.reservations[request.episode].test_phase_need, need)
                changed = replace(need, width=4, bytes=GIB)
                reader.return_value = changed
                _ = fixture.server.handle_request(wire.WidthRequest("shared-reader", grant.reservation, 4))
                fixture.server.resolve_estimates()
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
                policy = fixture.server.last_policy
                assert isinstance(inputs, gate.QueueInputs)
                assert isinstance(limits, gate.AdmissionLimits)
                self.assertEqual(result, gate.admission_pass(inputs, inputs.now, limits, policy=policy))
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
                            gate.queue_read_from(inputs, limits, policy=policy).view.waiting))
                expected = gate.queue_read_from(inputs, limits, policy=policy)
                with mock.patch.dict(os.environ, {"BUILDLOG_BUILD_BUDGET_GIB": "1"}):
                    self.assertEqual(fixture.server.handle_request(wire.ViewRequest()), [wire.ViewReply(expected)])
                    explained = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(explained, wire.ExplainReply)
                self.assertEqual(explained.explanation.decided_from, inputs)
                self.assertEqual(explained.explanation.limits, limits)

    def test_queries_project_the_stored_pass_without_another_admission_walk(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            inputs, limits = fixture.server.last_inputs, fixture.server.last_limits
            result, policy = fixture.server.last_result, fixture.server.last_policy
            assert isinstance(inputs, gate.QueueInputs)
            assert isinstance(limits, gate.AdmissionLimits)
            assert isinstance(result, gate.PassResult)
            projected = gate.queue_read_from_pass(inputs, limits, policy, result)
            self.assertEqual(projected, gate.queue_read_from(inputs, limits, policy))
            with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as walk:
                viewed = fixture.server.handle_request(wire.ViewRequest())
                explained = fixture.server.handle_request(wire.ExplainRequest())
                mine = fixture.server.handle_request(wire.MineRequest(request.identity.session_id))
            walk.assert_not_called()
            self.assertEqual(viewed, [wire.ViewReply(projected)])
            self.assertEqual(explained, [wire.ExplainReply(projected.explanation)])
            own = mine[0]
            assert isinstance(own, wire.MineReply)
            self.assertEqual(own.reading.explanation.decided_from, inputs)

    def test_cap_waiter_never_expires_even_with_the_fresh_test_flag(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            policy = fixture.server.last_policy
            request = fixture.request(age=policy.timing.head_seconds + 1)
            request = replace(request, profile=gate.ProgramProfile("merge-gate", "repo", "tree",
                gate.FixedCapSizing(6 * GIB)), fresh_test=True)
            replies = fixture.server.handle_request(request)
            held = replies[-1]
            assert isinstance(held, wire.Wait)
            self.assertNotIsInstance(held.reason, gate.EstimateExpired)
            fixture.clock.advance(policy.timing.head_seconds + 1)
            _ = fixture.server.run_pass()
            reply = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reply, wire.ViewReply)
            head = reply.reading.explanation.head
            assert isinstance(head, gate.HeadExplanation)
            self.assertNotIsInstance(head.waiter.reason, gate.EstimateExpired)
            self.assertEqual(head.waiter.need, 6 * GIB)
            self.assertIsInstance(head.need_source, gate.RecordedNeed)

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
        cases: tuple[tuple[gate.TestWidth, gate.Call, str], ...] = (
            (gate.RequestedTestWidth(3), gate.CallRecord("recorded"), "nextest"),
            (gate.UnspecifiedTestWidth(), gate.NoCallRecord(), "nextest"),
            (gate.NoTestWidth(), gate.NoCallRecord(), "clippy"))
        for width, call, step in cases:
            with self.subTest(width=width), AdmissionFixture() as fixture:
                request = fixture.request(width=width, call=call, step=step)
                granted = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                row = service.reservation_row(record)
                self.assertEqual(row["test_threads"], width)
                self.assertEqual(row["call_id"], call)
                self.assertEqual(service.reservation_record(request.episode, record.path, row), record)
                fixture.restart()
                self.assertEqual(fixture.server.reservations[request.episode], record)
                self.assertNotIn('null', fixture.state_text())
                self.assertEqual(only_admitted(fixture.server.handle_request(request)), granted)
                self.assertEqual(len(fixture.server.reservations), 1)

    def test_running_and_idle_views_refresh_at_the_configured_interval(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "lint.conf"
            _ = write_config_fixture(config, "running_pass_seconds=3\n")
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            need = gate.TestPhaseNeed(2 * GIB, 5, 8, 8, "same_width")
            with mock.patch.object(gate, "test_phase_need", return_value=need):
                granted = only_admitted(fixture.server.handle_request(request))
                fixture.server.resolve_estimates()
            self.assertEqual(fixture.server.reservations[request.episode].test_phase_need, need)
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
            self.assertEqual(fixture.readings.counts["host"], 1)
            fixture.server.run_once(0)
            self.assertEqual(fixture.readings.counts["host"], 1)

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
            self.assertEqual(granted.reservation, record.reservation_id)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertGreaterEqual(gate.build_charge(inputs.running[0].measured), request.profile.peak.bytes)

    def test_release_crashes_finish_once_from_saved_intent_or_history(self) -> None:
        for boundary in ("after_state_write", "after_finalization_intent", "after_history_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                granted = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(granted)
                record = fixture.server.reservations[request.episode]
                release = wire.ReleaseRequest("release-crashed", granted.reservation, 137)
                if boundary == "after_finalization_intent":
                    fixture.fault.stage = boundary
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.server.handle_request(release)
                    saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                    self.assertIsInstance(saved.reservations[request.episode].finalization, dict)
                    self.assertFalse(list(fixture.state_dir.glob("*.reservation")))
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
                self.assertFalse(Path(record.path).exists())
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
                self.assertEqual(row.get("priority_rank"), 1)
                self.assertNotIn("head_since", row)
                self.assertNotIn("head_state", row)
            for forbidden in ('"head_since"', '"head_state"', '"bump_until"', '"bump_ends_at"'):
                self.assertNotIn(forbidden, fixture.state_text())

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
                lifetime = fixture.proc.lifetime(row, fixture.proc.owner_start(owner.pid) == service.LiveOwnerIdentity(owner.start_time))
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

    def test_serve_defaults_to_the_private_admission_directory(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            with mock.patch.object(sys, "argv", ["admission_service", "serve"]), \
                 mock.patch.object(service, "SystemClock", return_value=fixture.clock), \
                 mock.patch.object(service, "SystemReadings", return_value=fixture.readings), \
                 mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(service.AdmissionServer, "serve_forever") as serve:
                self.assertEqual(service.main(), 0)
            serve.assert_called_once_with()
            self.assertTrue((fixture.root / "log/admission/state.json").exists())


class CutoverServiceTests(unittest.TestCase):
    def test_admit_head_keeps_free_memory_and_pressure_guards_and_ends_only_its_own_override(self) -> None:
        for free, pressure, admitted in ((14, 0, False), (15, 0, True), (40, 6, False)):
            with self.subTest(free=free, pressure=pressure), AdmissionFixture() as fixture:
                holder = fixture.request(need=20 * GIB, owner=fixture.proc.spawn(20 * GIB))
                _ = only_admitted(fixture.server.handle_request(holder))
                fixture.free(free * GIB)
                head = fixture.request(need=12 * GIB)
                follower = fixture.request(need=12 * GIB, since=head.since + 1)
                _ = fixture.server.handle_request(head)
                _ = fixture.server.handle_request(follower)
                with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(pressure) if pressure else gate.NoPressureHold()), \
                     mock.patch.object(gate, "head_override_event", wraps=gate.head_override_event) as events:
                    self.assertEqual(fixture.server.handle_request(
                        wire.AdmitHeadRequest("override", "user asked", "showrunner-id")), [wire.Ok()])
                self.assertEqual([call.args[0] for call in events.call_args_list],
                                 ["set", "admitted"] if admitted else ["set"])
                record = cast(gate.HeadAdmissionOverride, events.call_args_list[0].args[1])
                self.assertEqual((record["pid"], record["start_time"], record["need"], record["free"],
                                  record["reserve"], record["why"], record["set_by"]),
                                 (head.owner.pid, head.owner.start_time, 12 * GIB, free * GIB,
                                  3 * GIB, "user asked", "showrunner-id"))
                self.assertEqual(head.episode in fixture.server.reservations, admitted)
                self.assertIsInstance(fixture.server.waiters[follower.episode].last_reply, wire.Wait)
                result = fixture.server.last_result
                assert isinstance(result, gate.PassResult)
                decision = next(row.decision for row in result.rows if row.owner == gate.waiter_owner(
                    {"pid": head.owner.pid, "start_time": head.owner.start_time, "need": 12 * GIB,
                     "since": head.since}))
                self.assertEqual(decision.budget, 21 * GIB)
                self.assertGreater(decision.charged + 12 * GIB, decision.budget)
                self.assertEqual(isinstance(fixture.server.override, gate.NoHeadAdmissionOverride), admitted)
                if pressure:
                    self.assertIsInstance(decision.pressure, gate.MemoryPressure)

    def test_replacing_admit_head_replays_once_and_owner_exit_lapses_it_without_transfer(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            first = fixture.request(since=fixture.clock.now() - 10)
            second = fixture.request()
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(second)
            with mock.patch.object(gate, "head_override_event", wraps=gate.head_override_event) as events:
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("first", "first", "director"))
                replacement = wire.AdmitHeadRequest("replacement", "replacement", "director")
                _ = fixture.server.handle_request(replacement)
                _ = fixture.server.handle_request(replacement)
                override = fixture.server.override
                assert not isinstance(override, gate.NoHeadAdmissionOverride)
                self.assertEqual((override["pid"], override["start_time"], override["why"]),
                                 (first.owner.pid, first.owner.start_time, "replacement"))
                fixture.proc.kill(first.owner)
                fixture.server.run_once(0)
            self.assertEqual([call.args[0] for call in events.call_args_list], ["set", "set", "lapsed"])
            self.assertIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)
            self.assertIsInstance(fixture.server.waiters[second.episode].last_reply, wire.Wait)
            self.assertNotIn(first.episode, fixture.server.waiters)

    def test_pressure_held_override_has_the_same_view_explanation_and_frozen_release_result(self) -> None:
        with AdmissionFixture() as fixture:
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=20 * GIB, owner=fixture.proc.spawn(20 * GIB))))
            fixture.free(23 * GIB)
            head = fixture.request(need=12 * GIB)
            _ = fixture.server.handle_request(head)
            with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(6)):
                _ = fixture.server.handle_request(wire.AdmitHeadRequest("override", "urgent", "director"))
            before = fixture.state_text()
            viewed = fixture.server.handle_request(wire.ViewRequest())[0]
            explained = fixture.server.handle_request(wire.ExplainRequest())[0]
            assert isinstance(viewed, wire.ViewReply) and isinstance(explained, wire.ExplainReply)
            self.assertEqual(viewed.reading.explanation, explained.explanation)
            current = explained.explanation.head
            assert isinstance(current, gate.HeadExplanation)
            self.assertEqual(current.waiter.reason, viewed.reading.view.waiting[0].reason)
            self.assertEqual(current.decision.state, "hold")
            self.assertIsInstance(gate.smallest_head_release(explained.explanation), gate.HeadStillHeld)
            self.assertEqual(fixture.state_text(), before)
            self.assertIn(head.episode, fixture.server.waiters)
            self.assertNotIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)

    def test_release_records_each_closed_outcome_once_and_removes_the_sampler_paths(self) -> None:
        for status, outcome in ((0, "completed"), (1, "failed"), (127, "failed"),
                                (128, "killed"), (137, "killed")):
            with self.subTest(status=status), AdmissionFixture() as fixture:
                request = fixture.request(need=12 * GIB, step="build")
                request = replace(request, argv=("cargo", "build", "--release", "--bin", "fixture"))
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                granted = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                fixture.scoped_peak(granted, 10 * GIB)
                release = wire.ReleaseRequest("once", granted.reservation, status)
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                row, = fixture.history()
                self.assertEqual((row["status"], row["outcome"], row["need"], row["reservation_id"]),
                                 (status, outcome, 12 * GIB, granted.reservation))
                for path in (record.path, granted.max_path, granted.wave_path, record.sidecar):
                    self.assertFalse(Path(path).exists(), path)
                self.assertEqual(row["shape"], build_shape(list(request.argv)).key)
                self.assertFalse(fixture.server.reservations)

    def test_two_release_builds_in_one_call_keep_shape_and_separate_history(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ("cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana")
            ids: list[str] = []
            for index in range(2):
                request = fixture.request(need=12 * GIB, step="build", call=gate.CallRecord("one-call"))
                request = replace(request, argv=argv, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                ids.append(grant.reservation)
                record = fixture.server.reservations[request.episode]
                self.assertEqual(record.shape, build_shape(list(argv)).key)
                fixture.scoped_peak(grant, (10 + index) * GIB)
                release = wire.ReleaseRequest(f"release-{index}", grant.reservation, 0)
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            rows = fixture.history()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["reservation_id"] for row in rows], ids)
            self.assertEqual([row["call_id"] for row in rows], ["one-call", "one-call"])
            self.assertEqual([row["anon_peak_bytes"] for row in rows], [10 * GIB, 11 * GIB])
            self.assertTrue(all(row["shape"] == build_shape(list(argv)).key for row in rows))

    def test_unfinished_history_append_retries_the_saved_outcome_once(self) -> None:
        from scripts.lint.test_memory_admit import stop_history_append, HISTORY_APPEND_STOPS
        for tail in HISTORY_APPEND_STOPS:
            with self.subTest(tail=tail), AdmissionFixture() as fixture:
                request = fixture.request(step="build")
                request = replace(request, sidecar=str(fixture.scope(request.owner)),
                    profile=replace(request.profile, worktree="build-Δ"))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant, 15 * GIB)
                release = wire.ReleaseRequest("stopped-release", grant.reservation, 137)
                with stop_history_append(gate.store, tail):
                    with self.assertRaisesRegex(OSError, "injected stop"):
                        _ = fixture.server.handle_request(release)
                saved = service.read_state(fixture.state_dir).reservations[request.episode]
                self.assertIsInstance(saved.finalization, dict)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(replace(release, status=0)), [wire.Ok()])
                row, = fixture.history()
                self.assertEqual((row["outcome"], row["status"], row["anon_peak_bytes"]),
                                 ("killed", 137, 15 * GIB))
                self.assertEqual(row["worktree"], "build-Δ")
                self.assertFalse(fixture.server.reservations)

    def test_saved_finalization_keeps_scope_evidence_after_sidecar_removal(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 14 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("stop", grant.reservation, 137))
            Path(request.sidecar).unlink()
            fixture.restart()
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["anon_peak_bytes"]), ("killed", 14 * GIB))
            self.assertFalse(fixture.server.reservations)

    def test_peak_read_error_keeps_all_sampler_evidence_for_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 14 * GIB)
            _ = Path(grant.wave_path).write_text(f"{14 * GIB} {fixture.clock.now()}")
            paths = (Path(grant.max_path), Path(grant.wave_path), Path(request.sidecar))
            before = {path: path.read_bytes() for path in paths}
            read = Path.read_text
            def fail_peak(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                if path == Path(grant.max_path):
                    raise PermissionError("the peak is unreadable")
                return read(path, encoding=encoding, errors=errors)
            with mock.patch.object(Path, "read_text", fail_peak):
                with self.assertRaisesRegex(PermissionError, "peak is unreadable"):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("retry", grant.reservation, 0))
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertFalse(fixture.history())
            fixture.restart()
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 14 * GIB)
            self.assertFalse(fixture.server.reservations)

    def test_cleanup_retries_after_each_sampler_file_removal_without_duplicate_history(self) -> None:
        for suffix in (".max", ".wave", ".cgroup"):
            with self.subTest(suffix=suffix), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant)
                stop = Path(request.sidecar) if suffix == ".cgroup" else Path(grant.max_path).with_suffix(suffix)
                unlink = Path.unlink
                def stop_after_removal(path: Path, missing_ok: bool = False) -> None:
                    unlink(path, missing_ok=missing_ok)
                    if path == stop:
                        raise InterruptedError("cleanup stopped")
                with mock.patch.object(Path, "unlink", stop_after_removal):
                    with self.assertRaisesRegex(InterruptedError, "cleanup stopped"):
                        _ = fixture.server.handle_request(wire.ReleaseRequest("cleanup", grant.reservation, 0))
                self.assertEqual(len(fixture.history()), 1)
                fixture.restart()
                self.assertEqual(len(fixture.history()), 1)
                self.assertFalse(fixture.server.reservations)
                for path in (Path(grant.max_path), Path(grant.wave_path), Path(request.sidecar)):
                    self.assertFalse(path.exists(), str(path))

    def test_unscoped_release_writes_no_peak_history(self) -> None:
        with AdmissionFixture() as fixture:
            grant = only_admitted(fixture.server.handle_request(fixture.request()))
            _ = Path(grant.max_path).write_text(str(14 * GIB))
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("unscoped", grant.reservation, 0)), [wire.Ok()])
            self.assertFalse(fixture.history())
            self.assertFalse(Path(grant.max_path).exists())

    def test_strict_room_counts_committed_backfill_until_the_original_blockers_end(self) -> None:
        with AdmissionFixture() as fixture:
            blockers: list[tuple[wire.AdmitRequest, wire.Admitted]] = []
            for _ in range(2):
                request = fixture.request(need=11 * GIB // 2)
                grant = only_admitted(fixture.server.handle_request(request))
                blockers.append((request, grant))
            fixture.clock.advance(1)
            fixture.free(0)
            head = fixture.request(need=12 * GIB,
                since=fixture.clock.now() - fixture.server.last_policy.timing.head_seconds)
            _ = fixture.server.handle_request(head)
            fixture.server.waiters[head.episode].row["priority_since"] = fixture.clock.now()
            fixture.free(60 * GIB)
            for amount in (4, 5):
                request = fixture.request(need=amount * GIB)
                _ = only_admitted(fixture.server.handle_request(request))
                self.assertIn(request.episode, fixture.server.reservations)
            held = fixture.request(need=GIB // 4)
            reply = fixture.server.handle_request(held)[0]
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance(reply.reason, gate.BehindHead)
            self.assertEqual(reply.snapshot.head_need, 12 * GIB)
            room = reply.snapshot.decision.head_room
            self.assertEqual(room, gate.HeadRoom(12 * GIB, 9 * GIB, 9 * GIB, False))
            view = fixture.server.handle_request(wire.ViewRequest())[0]
            explanation = fixture.server.handle_request(wire.ExplainRequest())[0]
            assert isinstance(view, wire.ViewReply) and isinstance(explanation, wire.ExplainReply)
            self.assertEqual(view.reading.view.waiting[-1].reason, reply.reason)
            self.assertEqual(explanation.explanation.line.steps[-1].waiter.reason, reply.reason)
            self.assertIsInstance(gate.smallest_head_release(explanation.explanation), gate.HeadAdmitsAfterEnd)
            first, second = blockers
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                "first-blocker", first[1].reservation, 0)), [wire.Ok()])
            self.assertNotIn(head.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                "second-blocker", second[1].reservation, 0)), [wire.Ok()])
            self.assertIn(head.episode, fixture.server.reservations)
            self.assertNotIn(head.episode, fixture.server.waiters)
            place, = [place for place in fixture.server.places.values() if place.owner == head.owner]
            self.assertEqual(place.episode, head.episode)

    def test_priority_override_and_frozen_release_keep_the_resolved_head(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            older = fixture.request(need=20 * GIB, since=fixture.clock.now() - 600)
            chosen = fixture.request(need=12 * GIB, since=fixture.clock.now() - 60)
            for request in (older, chosen):
                _ = fixture.server.handle_request(request)
            with mock.patch.object(gate, "priority_labels", return_value={
                    older.identity.session_id: UnitLabels("old", unit="other"),
                    chosen.identity.session_id: UnitLabels("chosen", unit="chosen")}):
                self.assertEqual(fixture.server.handle_request(wire.PrioritySetRequest(
                    "choose", "chosen", "urgent", "director")), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(wire.AdmitHeadRequest(
                    "head", "checked free memory", "director")), [wire.Ok()])
                override = fixture.server.override
                assert not isinstance(override, gate.NoHeadAdmissionOverride)
                self.assertEqual(override["pid"], chosen.owner.pid)
                explanation = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(explanation, wire.ExplainReply)
                self.assertEqual(explanation.explanation.ordered[0]["pid"], chosen.owner.pid)
                head = explanation.explanation.head
                assert isinstance(head, gate.HeadExplanation)
                self.assertEqual(head.waiter.pid, chosen.owner.pid)
                frozen = explanation.explanation
                with mock.patch.object(gate, "priority_labels", side_effect=AssertionError("frozen roster")), \
                     mock.patch.object(Path, "read_text", side_effect=AssertionError("frozen files")):
                    self.assertIsInstance(gate.smallest_head_release(frozen), gate.HeadStillHeld)
                    self.assertIsInstance(gate.head_after_steps_end(frozen, ()), gate.HeadStillHeld)
                fixture.free(60 * GIB)
                _ = fixture.server.run_pass()
                self.assertIn(chosen.episode, fixture.server.reservations)
                self.assertIsInstance(fixture.server.override, gate.NoHeadAdmissionOverride)

    def test_policy_edits_reclassify_pressure_and_kind_peaks_in_both_directions(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="merge-gate", need=8 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 8 * GIB)
            policy = fixture.server.last_policy
            fixture.clock.advance(policy.timing.ramp_seconds + 1000)
            _ = Path(grant.wave_path).write_text(f"{8 * GIB} {int(fixture.clock.now() - 200)}")
            for old, new, charged in ((180, 300, 8 * GIB), (300, 180, GIB)):
                fixture.configure(**{"peak_hold_seconds.merge-gate": old})
                _ = fixture.server.run_pass()
                before = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(before, wire.ExplainReply)
                self.assertNotEqual(before.explanation.running[0].charged, charged)
                fixture.configure(**{"peak_hold_seconds.merge-gate": new})
                _ = fixture.server.run_pass()
                after = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(after, wire.ExplainReply)
                self.assertEqual(after.explanation.running[0].charged, charged)
                self.assertNotEqual(before.explanation.running[0].charged, charged)
            pressure = Path(os.environ["BUILDLOG_MEMORY_PRESSURE"])
            fixture.free(0)
            _ = pressure.write_text("full avg10=7.0 avg60=0 avg300=0 total=0\n")
            waiting = fixture.request()
            _ = fixture.server.handle_request(waiting)
            for old, new, held in ((5, 10, False), (10, 5, True)):
                fixture.configure(full_pressure_limit=old)
                _ = fixture.server.run_pass()
                before = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(before, wire.ExplainReply)
                fixture.configure(full_pressure_limit=new)
                _ = fixture.server.run_pass()
                after = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(after, wire.ExplainReply)
                self.assertEqual(isinstance(before.explanation.decided_from.pressure, gate.MemoryPressure), not held)
                self.assertEqual(isinstance(after.explanation.decided_from.pressure, gate.MemoryPressure), held)

    def test_ramp_growth_and_release_keep_peak_width_call_and_repository(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB, width=gate.RequestedTestWidth(16), call=gate.CallRecord("call-width-16"))
            request = replace(request, sidecar=str(fixture.scope(request.owner)),
                profile=gate.StepProfile("hana", "nextest", "tree", gate.ExpectedPeak(12 * GIB, "measured", 5),
                    gate.RepositoryIdentity("https://example.org/hana", "hana"), "full", fixture.clock.now()))
            scope = Path(Path(request.sidecar).read_text())
            _ = (scope / "memory.current").write_text(str(8 * GIB))
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            reading = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reading, wire.ViewReply)
            running = reading.reading.view.running[0].measured
            assert isinstance(running, gate.MeasuredMemory)
            self.assertEqual((gate.growth_headroom(running), running.bytes), (4 * GIB, 8 * GIB))
            fixture.scoped_peak(grant, 8 * GIB)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("end-ramp", grant.reservation, 7)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual((row["anon_peak_bytes"], row["status"], row["test_threads"], row["call_id"]),
                (8 * GIB, 7, 16, "call-width-16"))
            self.assertEqual((row["repo_identity"], row["test_scope"]), ("https://example.org/hana", "full"))
            self.assertFalse(Path(request.sidecar).exists())
            self.assertFalse(Path(grant.max_path).exists())
            self.assertFalse(fixture.server.reservations)

    def test_malformed_legacy_priority_does_not_stop_import_or_admission(self) -> None:
        for content in ("{broken", "[]", '{"pid":"bad"}'):
            with self.subTest(content=content), AdmissionFixture() as fixture:
                fixture.server.close()
                (fixture.state_dir / "state.json").unlink()
                path = gate.queue_priority_path(fixture.state_dir)
                _ = path.write_text(content)
                with mock.patch("sys.stderr", io.StringIO()) as errors:
                    fixture.server = fixture.new_server()
                self.assertEqual(len(errors.getvalue().splitlines()), 1, errors.getvalue())
                self.assertIsInstance(fixture.server.priority, gate.NoQueuePriority)
                _ = only_admitted(fixture.server.handle_request(fixture.request()))
                self.assertFalse(path.exists())

    def test_priority_requires_a_waiting_or_reserved_unit_and_replays_refusals(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), AdmissionFixture() as fixture:
                request = fixture.request()
                fixture.free(60 * GIB if running else 0)
                _ = fixture.server.handle_request(request)
                absent = wire.PrioritySetRequest("absent", "missing", "urgent", "director")
                with mock.patch.object(gate, "priority_labels", return_value={
                        "unrelated": UnitLabels("other", unit="missing"),
                        request.identity.session_id: UnitLabels("worker", unit="present")}):
                    refused = fixture.server.handle_request(absent)
                    self.assertEqual(refused, [wire.Refused(
                        "no waiting step or current reservation belongs to unit missing")])
                    self.assertIsInstance(fixture.server.priority, gate.NoQueuePriority)
                    self.assertEqual(fixture.server.handle_request(wire.PrioritySetRequest(
                        "present", "present", "urgent", "director")), [wire.Ok()])
                    self.assertEqual(fixture.server.handle_request(absent), refused)
                    priority = fixture.server.priority
                    assert not isinstance(priority, gate.NoQueuePriority)
                    self.assertEqual(priority["unit"], "present")

    def test_waiter_keeps_source_basis_scope_and_typed_width_in_state_and_views(self) -> None:
        for source, runs in (("measured", 7), ("buildlog", 12), ("pooled", 31), ("fallback", 0)):
            with self.subTest(source=source), AdmissionFixture() as fixture:
                fixture.free(0)
                request = fixture.request(width=gate.RequestedTestWidth(8))
                assert isinstance(request.profile, gate.StepProfile)
                request = replace(request, argv=("cargo", "nextest", "run", "-E",
                    "package(hana) & (test(one) | test(two))", "--test-threads", "8"),
                    profile=replace(request.profile, scope="filtered", repository=gate.RepositoryIdentity(
                        "https://example.org/hana", "hana"), peak=gate.ExpectedPeak(12 * GIB, source, runs)))
                _ = fixture.server.handle_request(request)
                row = fixture.server.waiters[request.episode].row
                self.assertEqual((row.get("need_source"), row.get("need_runs"), row["need"]), (source, runs, 12 * GIB))
                self.assertEqual((row.get("test_scope"), row.get("package"), row.get("filters")), ("filtered", "hana", ["one", "two"]))
                self.assertEqual(row.get("estimated_at"), fixture.clock.now())
                fixture.restart()
                _ = fixture.server.handle_request(request)
                saved = fixture.server.waiters[request.episode].row
                self.assertEqual(saved, row)
                reply = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(reply, wire.ExplainReply)
                head = reply.explanation.head
                assert isinstance(head, gate.HeadExplanation)
                assert isinstance(head.need_source, gate.RecordedNeed)
                self.assertEqual((head.need_source.source, head.need_source.runs, head.need_source.basis),
                    (source, runs, gate.read_need_basis(row.get("need_basis"))))
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request(step="clippy", width=gate.NoTestWidth())
            _ = fixture.server.handle_request(request)
            stored_request = fixture.server.waiters[request.episode].request
            assert isinstance(stored_request, wire.AdmitRequest)
            self.assertIsInstance(stored_request.test_width, gate.NoTestWidth)

    def test_legacy_head_fields_do_not_survive_import_or_new_grants(self) -> None:
        import json
        with AdmissionFixture() as fixture:
            fixture.server.close()
            (fixture.state_dir / "state.json").unlink()
            request = fixture.request(since=fixture.clock.now() - 900)
            row = {"pid": request.owner.pid, "start_time": request.owner.start_time,
                "need": 8 * GIB, "since": request.since, "head_since": fixture.clock.now() - 10,
                "head_state": "head", "session_id": request.identity.session_id}
            legacy = fixture.state_dir / "legacy.waiting"
            _ = legacy.write_text(json.dumps(row))
            fixture.free(0)
            fixture.server = fixture.new_server()
            self.assertFalse(legacy.exists())
            for name in ("head_since", "head_state"):
                self.assertNotIn(name, fixture.state_text())
            _ = fixture.server.handle_request(request)
            view = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(view, wire.ViewReply)
            self.assertEqual(view.reading.view.waiting[0].since, request.since)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            self.assertIn(request.episode, fixture.server.reservations)
            for name in ("head_since", "head_state"):
                self.assertNotIn(name, fixture.state_text())

    def test_priority_clear_and_replacement_replay_each_transition_once_after_a_crash(self) -> None:
        for action in ("clear", "replace"):
            with self.subTest(action=action), AdmissionFixture() as fixture:
                fixture.free(0)
                request = fixture.request()
                _ = fixture.server.handle_request(request)
                _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value={
                    request.identity.session_id: UnitLabels("worker", unit="old")}))
                self.assertEqual(fixture.server.handle_request(
                    wire.PrioritySetRequest("initial", "old", "initial", "director")), [wire.Ok()])
                mutation = (wire.PriorityClearRequest("change", "director") if action == "clear" else
                            wire.PrioritySetRequest("change", "new", "replacement", "director"))
                if action == "replace":
                    _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value={
                        request.identity.session_id: UnitLabels("worker", unit="new")}))
                with mock.patch.object(gate.record, "queue_priority", wraps=gate.record.queue_priority) as logged:
                    fixture.fault.stage = "after_state_write"
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.server.handle_request(mutation)
                    logged.assert_not_called()
                    fixture.restart()
                    self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                    fixture.restart()
                    self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                logged.assert_called_once()
                self.assertEqual(logged.call_args.kwargs["transition_id"], mutation.request_id)
                if action == "clear":
                    self.assertIsInstance(fixture.server.priority, gate.NoQueuePriority)
                else:
                    priority = fixture.server.priority
                    assert not isinstance(priority, gate.NoQueuePriority)
                    self.assertEqual(priority["unit"], "new")

    def test_unowned_scope_memory_refreshes_without_a_waiter_or_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "builds.slice/unowned.scope"
            scope.mkdir(parents=True)
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "memory.current").write_text(str(GIB))
            _ = fixture.server.run_pass()
            first = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(first, wire.ViewReply)
            measured = first.reading.view.running[0].measured
            assert isinstance(measured, gate.UnownedSliceMemory)
            self.assertEqual(measured.bytes, GIB)
            _ = (scope / "memory.current").write_text(str(17 * GIB))
            fixture.clock.advance(gate.read_memory_config().running_pass_seconds)
            fixture.server.run_once(0)
            updated = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(updated, wire.ViewReply)
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            measured = updated.reading.view.running[0].measured
            assert isinstance(measured, gate.UnownedSliceMemory)
            self.assertEqual(measured.bytes, 17 * GIB)

    def test_finalization_crash_after_row_removal_expires_the_orphan_replay(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            def crash_after_removal(stage: str) -> None:
                if stage != "after_state_write":
                    return
                saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                if not saved.reservations and request.episode in saved.ledger:
                    raise InjectedCrash("reservation removed before replay end")
            with mock.patch.object(fixture.server, "fault", side_effect=crash_after_removal):
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            self.assertEqual(len(fixture.history()), 1)
            fixture.restart()
            self.assertFalse(fixture.server.reservations)
            fixture.clock.advance(2 * gate.read_memory_config().replay_retention_seconds)
            fixture.server.persist()
            self.assertNotIn(request.episode, fixture.server.ledger)
            fixture.restart()
            self.assertNotIn(request.episode, fixture.server.ledger)
            self.assertEqual(len(fixture.history()), 1)

    def test_fixed_cap_stays_charged_after_peak_decay_and_restart(self) -> None:
        from scripts.build_queue import queue_evidence
        from scripts.lib.user_zone import user_zone
        from scripts.build_queue.queue_fixture import write_memory_config
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "lint.conf", **{"peak_hold_seconds.merge-gate": "180"})
            request = fixture.request(step="merge-gate")
            request = replace(request, sidecar=str(fixture.scope(request.owner)),
                profile=gate.ProgramProfile("merge-gate", "repo", "tree", gate.FixedCapSizing(6 * GIB)),
                fresh_test=True)
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 5 * GIB)
            _ = Path(grant.wave_path).write_text(f"{5 * GIB} {int(fixture.clock.now())}")
            record = fixture.server.reservations[request.episode]
            scope = Path(Path(record.sidecar).read_text())
            _ = (scope / "memory.stat").write_text(f"anon {GIB}\n")
            policy = gate.read_memory_config().policy
            fixture.clock.advance(max(policy.timing.ramp_seconds,
                policy.timing.head_seconds, policy.peak_hold.seconds_for("merge-gate")) + 1)
            fixture.free(40 * GIB)
            waiting = fixture.request(need=20 * GIB)
            _ = fixture.server.handle_request(waiting)
            for restart in (False, True):
                if restart:
                    fixture.restart()
                    _ = fixture.server.handle_request(waiting)
                else:
                    _ = fixture.server.run_pass()
                viewed = fixture.server.handle_request(wire.ViewRequest())[0]
                explained = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(viewed, wire.ViewReply)
                assert isinstance(explained, wire.ExplainReply)
                self.assertEqual(viewed.reading.explanation, explained.explanation)
                running, = explained.explanation.running
                self.assertEqual((running.basis, running.charged), (gate.LifetimePromise(6 * GIB), 6 * GIB))
                self.assertEqual(gate.growth_headroom(running.step.measured), 5 * GIB)
                head = explained.explanation.head
                assert isinstance(head, gate.HeadExplanation)
                self.assertEqual(head.decision.state, "hold")
                self.assertFalse(head.budget.passes)
                self.assertNotIsInstance(head.waiter.reason, gate.EstimateExpired)
                row, = explained.explanation.reservations
                waiter = row.get("queue_waiter", {})
                self.assertEqual(waiter.get("need_source"), "cap")
                self.assertEqual(gate.read_need_basis(waiter.get("need_basis")), gate.ProgramMemoryCap("merge-gate"))
                self.assertEqual(queue_evidence.charge_text(running.basis, user_zone(), explained.explanation.policy,
                    running.step.step), "merge gate promise: holds until the step ends")
                self.assertEqual(queue_evidence.need_text(gate.RecordedNeed("cap", 0, gate.HISTORY_DAYS,
                    "full", "", (), gate.WidthNotRecorded(), gate.read_need_basis(waiter.get("need_basis"))),
                    budget=explained.explanation.limits.budget, zone=user_zone()),
                    "the merge gate's memory cap")
                self.assertEqual(fixture.server.reservations[request.episode].need, 6 * GIB)

    def test_priority_replay_after_a_crash_keeps_one_transition_and_the_same_order(self) -> None:
        from scripts.message.unit_labels import UnitLabels
        with AdmissionFixture() as fixture:
            fixture.free(0)
            first, second = fixture.request(), fixture.request()
            labels = {first.identity.session_id: UnitLabels("first", unit="other"),
                      second.identity.session_id: UnitLabels("second", unit="queue")}
            with mock.patch.object(gate, "priority_labels", return_value=labels), \
                 mock.patch.object(gate.record, "queue_priority", wraps=gate.record.queue_priority) as logged:
                _ = fixture.server.handle_request(first)
                _ = fixture.server.handle_request(second)
                mutation = wire.PrioritySetRequest("priority-once", "queue", "urgent", "director")
                fixture.fault.stage = "after_state_write"
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(mutation)
                logged.assert_not_called()
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
                for request in (first, second):
                    _ = fixture.server.handle_request(request)
                fixture.restart()
                for request in (first, second):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            logged.assert_called_once()
            self.assertEqual(logged.call_args.kwargs["transition_id"], mutation.request_id)
            viewed = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(viewed, wire.ViewReply)
            self.assertEqual([row["pid"] for _, row in viewed.reading.ordered],
                             [second.owner.pid, first.owner.pid])
            self.assertEqual([row.get("priority_rank") for _, row in viewed.reading.ordered], [0, 1])
            priority = viewed.reading.explanation.priority
            assert not isinstance(priority, gate.NoQueuePriority)
            self.assertEqual(priority["unit"], "queue")

    def test_token_replay_after_a_crash_restores_the_place_and_records_one_gap(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request()
            first = replace(first, sidecar=str(fixture.scope(first.owner)))
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.scoped_peak(grant)
            before, = fixture.server.places.values()
            mutation = wire.TokenWaitRequest("token-once", grant.reservation)
            fixture.fault.stage = "after_state_write"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(mutation)
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            self.assertFalse(fixture.server.reservations)
            place, = fixture.server.places.values()
            self.assertEqual((place.owner, place.since, place.priority_since),
                             (before.owner, before.since, before.priority_since))
            self.assertIsInstance(place.token_left_at, float)
            self.assertEqual([row["reservation_id"] for row in fixture.history()], [grant.reservation])
            fixture.clock.advance(1)
            fixture.free(0)
            second = replace(first, episode="after-token", token_back_at=fixture.clock.now())
            _ = fixture.server.handle_request(second)
            _ = fixture.server.handle_request(second)
            row = fixture.server.waiters[second.episode].row
            self.assertEqual(row["since"], first.since)
            self.assertEqual(row.get("token_left_at"), place.token_left_at)
            fixture.restart()
            _ = fixture.server.handle_request(second)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                times = list(gate.queue_times_backward(stream))
            gaps = [row for row in times if row.get("kind") == "token_gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["episode"], second.episode)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            later = fixture.server.ledger[second.episode].reply
            assert isinstance(later, wire.Admitted)
            self.assertNotEqual(later.reservation, grant.reservation)

    def test_new_failed_build_peak_expires_matching_waiters_without_losing_their_place(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ("cargo", "build", "--release", "--bin", "fixture")
            repository = gate.RepositoryIdentity("https://example.org/fixture", "fixture")
            shape = build_shape(list(argv))
            profile = gate.StepProfile("fixture", "build", str(fixture.root),
                gate.ExpectedPeak(12 * GIB, "shape", 9,
                    gate.ShapeP90(9, gate.NoFloor(), gate.ReleaseHeadroom(), gate.UnderBudget())),
                repository, "full", fixture.clock.now())
            owner_request = fixture.request(need=12 * GIB, step="build")
            assert isinstance(owner_request.profile, gate.StepProfile)
            profile = replace(profile, build_history_generation=owner_request.profile.build_history_generation)
            owner_request = replace(owner_request, argv=argv, profile=profile,
                sidecar=str(fixture.scope(owner_request.owner)))
            grant = only_admitted(fixture.server.handle_request(owner_request))
            peak = int(15.76 * GIB)
            fixture.scoped_peak(grant, peak)
            fixture.free(0)
            waiter = replace(fixture.request(need=12 * GIB, step="build",
                since=fixture.clock.now() - 900), argv=argv, profile=profile)
            _ = fixture.server.handle_request(waiter)
            before = dict(fixture.server.waiters[waiter.episode].row)
            fixture.proc.kill(owner_request.owner)
            scope = Path(Path(owner_request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            _ = fixture.server.run_pass()
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["need"], row["shape"], row["anon_peak_bytes"]),
                ("owner_gone", 12 * GIB, shape.key, peak))
            waiting = fixture.server.waiters[waiter.episode]
            held = waiting.last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance(held.reason, gate.EstimateExpired)
            for key in ("since", "priority_since", "need", "need_basis"):
                self.assertEqual(waiting.row.get(key), before.get(key))
            self.assertNotIn(owner_request.episode, fixture.server.reservations)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            fixture.clock.advance(1)
            fixture.free(60 * GIB)
            blocker = fixture.request(need=GIB // 8, step="check",
                owner=fixture.proc.spawn(used=GIB // 8))
            blocker_grant = only_admitted(fixture.server.handle_request(blocker))
            fixture.free(18 * GIB)
            with test_hang_guard.LauncherHangGuard(self.id()).protect(), fixture.serving(), \
                    mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(gate, "git_identity", return_value=(repository, str(fixture.root))):
                refreshed = client.step_request(list(argv), str(fixture.root), waiter.owner,
                    waiter.since, waiter.sidecar, waiter.episode)
            self.assertEqual((refreshed.owner, refreshed.episode, refreshed.since),
                (waiter.owner, waiter.episode, waiter.since))
            self.assertIsInstance(refreshed.profile, gate.StepProfile)
            assert isinstance(refreshed.profile, gate.StepProfile)
            self.assertEqual(refreshed.profile.build_history_generation,
                gate.BuildHistoryGeneration(fixture.server.build_history_generation))
            self.assertGreater(refreshed.profile.peak.bytes, peak)
            self.assertEqual(fixture.server.reservations[blocker.episode].reservation_id,
                blocker_grant.reservation)
            next_attempt = replace(waiter, profile=refreshed.profile)
            reply, = fixture.server.handle_request(next_attempt)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertNotIsInstance(reply.reason, gate.EstimateExpired)
            self.assertGreater(reply.snapshot.peak.bytes, 15.76 * GIB)
            self.assertEqual(reply.snapshot.decision.state, "hold")
            self.assertEqual(reply.snapshot.peak, next_attempt.profile.peak)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            fixture.free(60 * GIB)
            admitted = only_admitted(fixture.server.handle_request(next_attempt))
            self.assertEqual(admitted.need, next_attempt.profile.peak.bytes)
            self.assertEqual(fixture.server.reservations[waiter.episode].need, next_attempt.profile.peak.bytes)

    def build_history_request(self, fixture: AdmissionFixture, *, since: float = 900) -> wire.AdmitRequest:
        request = fixture.request(need=12 * GIB, step="build", since=since)
        assert isinstance(request.profile, gate.StepProfile)
        profile = replace(request.profile, worktree=str(fixture.root),
            repository=gate.RepositoryIdentity("https://example.org/fixture", "fixture"),
            peak=gate.ExpectedPeak(12 * GIB, "shape", 9,
                gate.ShapeP90(9, gate.NoFloor(), gate.ReleaseHeadroom(), gate.UnderBudget())))
        return replace(request, argv=("cargo", "build", "--release", "--bin", "fixture"), profile=profile)

    def assert_unregistered_build_is_expired(self, *, restart: bool = False,
            corrected_clock: bool = False, longer_head: bool = False) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = 1000
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.clock.epoch = 1050
            stale = self.build_history_request(fixture)
            self.assertNotIn(stale.episode, fixture.server.waiters)
            self.assertFalse(any(place.owner == stale.owner for place in fixture.server.places.values()))
            assert isinstance(stale.profile, gate.StepProfile)
            generation = stale.profile.build_history_generation
            assert isinstance(generation, gate.BuildHistoryGeneration)
            fixture.clock.epoch = 1040 if corrected_clock else 1100
            _ = fixture.server.handle_request(wire.ReleaseRequest("published-before-connect", grant.reservation, 137))
            fixture.clock.advance(0.001)
            fixture.server.persist()
            if restart:
                fixture.restart()
            if longer_head:
                old_head = gate.read_memory_config().policy.timing.head_seconds
                fixture.clock.epoch = stale.profile.estimated_at + old_head + 2
                fixture.configure(head_seconds=old_head * 2)
                self.assertLess(fixture.clock.now() - stale.profile.estimated_at,
                    gate.read_memory_config().policy.timing.head_seconds)
            if corrected_clock:
                self.assertLess(fixture.clock.now(), stale.profile.estimated_at)
            history, = fixture.history()
            self.assertEqual(history["anon_peak_bytes"], 16 * GIB)
            self.assertGreater(service.read_state(fixture.state_dir).build_history_generation, generation.value)
            self.assertFalse(fixture.server.waiters)
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance(reply.reason, gate.EstimateExpired)
            self.assertNotIn(stale.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
            self.assertLess(stale.profile.peak.bytes + reply.snapshot.decision.reserve, 18 * GIB)

    def test_unregistered_build_estimate_expires_after_publication_and_persistence(self) -> None:
        self.assert_unregistered_build_is_expired()

    def test_unregistered_build_estimate_expires_after_publication_and_restart(self) -> None:
        self.assert_unregistered_build_is_expired(restart=True)

    def test_clock_correction_cannot_revive_a_build_estimate_after_publication(self) -> None:
        self.assert_unregistered_build_is_expired(corrected_clock=True)

    def test_larger_head_seconds_cannot_revive_a_build_estimate_after_publication(self) -> None:
        self.assert_unregistered_build_is_expired(longer_head=True)

    def test_build_admit_and_launch_require_a_current_history_generation(self) -> None:
        for launch in (False, True):
            for missing in (False, True):
                with self.subTest(launch=launch, missing=missing), AdmissionFixture() as fixture:
                    stale = self.build_history_request(fixture)
                    assert isinstance(stale.profile, gate.StepProfile)
                    if missing:
                        profile = replace(stale.profile, build_history_generation=gate.NoBuildHistoryGeneration())
                        stale = replace(stale, profile=profile)
                    else:
                        owner = self.build_history_request(fixture)
                        owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
                        grant = only_admitted(fixture.server.handle_request(owner))
                        fixture.scoped_peak(grant, 16 * GIB)
                        _ = fixture.server.handle_request(wire.ReleaseRequest("advance-before-launch", grant.reservation, 137))
                    fixture.free(18 * GIB)
                    request: wire.AdmitRequest | wire.LaunchRequest = stale
                    if launch:
                        request = wire.LaunchRequest(stale.episode, stale.owner, stale.since, stale.identity, stale.profile)
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance(reply.reason, gate.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations)
                    self.assertNotIn(stale.episode, fixture.server.ledger)

    def test_current_generation_refresh_preserves_owner_episode_and_queue_place(self) -> None:
        with AdmissionFixture() as fixture:
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            stale = self.build_history_request(fixture)
            _ = fixture.server.handle_request(wire.ReleaseRequest("peak-before-refresh", grant.reservation, 137))
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance(reply.reason, gate.EstimateExpired)
            before = dict(fixture.server.waiters[stale.episode].row)
            with test_hang_guard.LauncherHangGuard(self.id()).protect(), fixture.serving(), \
                    mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(gate, "git_identity", return_value=(stale.profile.repository, str(fixture.root))):
                refreshed = client.step_request(list(stale.argv), stale.cwd, stale.owner,
                    stale.since, stale.sidecar, stale.episode)
            self.assertEqual((refreshed.owner, refreshed.episode, refreshed.since),
                (stale.owner, stale.episode, stale.since))
            self.assertGreater(refreshed.profile.peak.bytes, stale.profile.peak.bytes)
            fixture.free(60 * GIB)
            granted = only_admitted(fixture.server.handle_request(refreshed))
            self.assertEqual(granted.need, refreshed.profile.peak.bytes)
            record = fixture.server.reservations[stale.episode]
            self.assertEqual(record.owner, stale.owner)
            for key in ("since", "priority_since"):
                self.assertEqual(record.queue_waiter.get(key), before.get(key))

    def test_retry_of_an_existing_history_row_advances_the_generation(self) -> None:
        with AdmissionFixture() as fixture:
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            release = wire.ReleaseRequest("retry-published-row", grant.reservation, 137)
            with mock.patch.object(service.StateIntents, "published", side_effect=OSError("generation save failed")):
                with self.assertRaisesRegex(OSError, "generation save failed"):
                    _ = fixture.server.handle_request(release)
            history, = fixture.history()
            self.assertEqual(history["reservation_id"], grant.reservation)
            stale = self.build_history_request(fixture)
            before = service.read_state(fixture.state_dir).build_history_generation
            fixture.restart()
            self.assertGreater(service.read_state(fixture.state_dir).build_history_generation, before)
            self.assertEqual(len(fixture.history()), 1)
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance(reply.reason, gate.EstimateExpired)
            self.assertNotIn(stale.episode, fixture.server.reservations)

    def test_delayed_build_refresh_cannot_clear_a_later_failed_peak(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = 1000
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            profile = owner.profile
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.free(0)
            age = gate.read_memory_config().policy.timing.head_seconds + 1
            waiter = replace(self.build_history_request(fixture),
                profile=replace(profile, estimated_at=1000 - age))
            first, = fixture.server.handle_request(waiter)
            assert isinstance(first, wire.Wait)
            self.assertIsInstance(first.reason, gate.EstimateExpired)
            before = dict(fixture.server.waiters[waiter.episode].row)
            delayed = replace(waiter, profile=replace(profile, estimated_at=1005))
            fixture.clock.epoch = 1010
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                "finished-after-refresh-read", grant.reservation, 137)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 16 * GIB)
            fixture.free(18 * GIB)
            fixture.clock.epoch = 1011
            reply, = fixture.server.handle_request(delayed)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance(reply.reason, gate.EstimateExpired)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.waiters[waiter.episode].row["since"], before["since"])
            self.assertLess(12 * GIB + reply.snapshot.decision.reserve, 18 * GIB)

    def test_pending_failed_peak_expires_estimates_before_and_after_restart_publication(self) -> None:
        for append_fails in (False, True):
            with self.subTest(append_fails=append_fails), AdmissionFixture() as fixture:
                fixture.clock.epoch = 1000
                owner = self.build_history_request(fixture)
                owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
                grant = only_admitted(fixture.server.handle_request(owner))
                fixture.scoped_peak(grant, 16 * GIB)
                fixture.clock.epoch = 1010
                fixture.fault.stage = "after_finalization_intent"
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("pending-failed-peak", grant.reservation, 137))
                saved = service.read_state(fixture.state_dir).reservations[owner.episode]
                intent = saved.finalization
                assert not isinstance(intent, service.NoFinalizationIntent)
                self.assertEqual(datetime.fromisoformat(intent["ended_at"]).timestamp(), 1010)
                self.assertEqual(fixture.history(), [])

                fixture.clock.epoch = 1050
                stale = self.build_history_request(fixture)
                assert isinstance(stale.profile, gate.StepProfile)
                self.assertEqual(stale.profile.build_history_generation, fixture.build_history_generation())
                fixture.free(18 * GIB)
                with self.subTest(boundary="pending finalization"), \
                        mock.patch.object(gate.store, "append_locked", side_effect=OSError("history unavailable")):
                    reply, = fixture.server.handle_request(stale)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance(reply.reason, gate.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations,
                        "a pending failed peak must prevent admission with the stale need")
                    self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
                self.assertEqual(fixture.history(), [])

                if append_fails:
                    fixture.clock.epoch = 1075
                    self.assertEqual(fixture.proc.owner_start(owner.owner.pid),
                        service.LiveOwnerIdentity(owner.owner.start_time))
                    with mock.patch.object(gate.store, "append_locked",
                            side_effect=OSError("history append failed")) as append:
                        fixture.restart()
                        with self.subTest(boundary="failed append after restart"):
                            reply, = fixture.server.handle_request(stale)
                            self.assertIsInstance(reply, wire.Wait)
                            assert isinstance(reply, wire.Wait)
                            self.assertIsInstance(reply.reason, gate.EstimateExpired)
                            self.assertNotIn(stale.episode, fixture.server.reservations,
                                "a failed history append must prevent stale admission")
                            retained = service.read_state(fixture.state_dir).reservations[owner.episode]
                            self.assertEqual(retained.finalization, intent)
                            self.assertEqual(fixture.history(), [])
                            self.assertGreaterEqual(append.call_count, 2,
                                "restart and admission must retry the pending append")

                fixture.clock.epoch = 1100
                if append_fails:
                    _ = fixture.server.run_pass()
                    self.assertEqual(fixture.proc.owner_start(owner.owner.pid),
                        service.LiveOwnerIdentity(owner.owner.start_time))
                else:
                    fixture.restart()
                history, = fixture.history()
                self.assertEqual((history["reservation_id"], history["anon_peak_bytes"]),
                    (grant.reservation, 16 * GIB))
                self.assertNotIn(owner.episode, fixture.server.reservations)
                with self.subTest(boundary="history published after restart"):
                    reply, = fixture.server.handle_request(stale)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance(reply.reason, gate.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations,
                        "publication must invalidate estimates made after the intent and before the append")
                    self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
                    self.assertLess(stale.profile.peak.bytes + reply.snapshot.decision.reserve, 18 * GIB)

    def test_rollback_export_finalizes_an_ended_reservation_before_removing_state(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("unfinished-export", grant.reservation, 137))
            fixture.proc.kill(request.owner)
            scope = Path(Path(request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.server.close()
            self.assertEqual(fixture.history(), [])
            finalize = gate.finalize_reservation
            def record_before_retirement(path: Path, row: gate.Reservation, outcome: gate.FinalOutcome,
                    now: datetime, status: int | gate.OutcomeNotRecorded = gate.OUTCOME_NOT_RECORDED, *, intents: gate.FinalizationIntents
                    ) -> gate.HistoryRecorded | gate.NoHistoryRecorded:
                self.assertTrue((fixture.state_dir / "state.json").exists())
                return finalize(path, row, outcome, now, status, intents=intents)
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(gate, "finalize_reservation", side_effect=record_before_retirement) as finished:
                service.export_priority(fixture.state_dir)
            self.assertEqual(finished.call_count, 1)
            row, = fixture.history()
            self.assertEqual((row["reservation_id"], row["anon_peak_bytes"], row["status"]),
                (grant.reservation, 16 * GIB, 137))
            self.assertFalse((fixture.state_dir / "state.json").exists())

    def test_rollback_export_keeps_state_when_pending_finalization_fails(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("failed-export", grant.reservation, 137))
            fixture.proc.kill(request.owner)
            scope = Path(Path(request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.server.close()
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(gate, "finalize_reservation", side_effect=OSError("history append failed")):
                with self.assertRaisesRegex(ValueError, grant.reservation):
                    service.export_priority(fixture.state_dir)
            saved = service.read_state(fixture.state_dir)
            self.assertIn(request.episode, saved.reservations)
            self.assertIsInstance(saved.reservations[request.episode].finalization, dict)
            self.assertEqual(fixture.history(), [])

    def test_cutover_rollback_and_second_cutover_keep_one_priority(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value={
                request.identity.session_id: UnitLabels("worker", unit="queue")}))
            mutation = wire.PrioritySetRequest("keep-priority", "queue", "urgent", "director")
            self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            expected = fixture.server.priority
            fixture.proc.kill(request.owner)
            _ = fixture.server.run_pass()
            fixture.server.close()
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc):
                service.export_priority(fixture.state_dir)
            legacy = gate.queue_priority_path(fixture.state_dir)
            self.assertTrue(legacy.exists())
            self.assertEqual(gate.load_queue_priority(fixture.state_dir), expected)
            self.assertFalse((fixture.state_dir / "state.json").exists())
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc):
                service.export_priority(fixture.state_dir)
            assert isinstance(expected, dict)
            changed = {**expected, "unit": "replacement", "why": "changed during rollback"}
            write_text_atomic(legacy, wire.encode_record(changed))
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc):
                service.export_priority(fixture.state_dir)
            self.assertEqual(gate.load_queue_priority(fixture.state_dir), changed)
            fixture.server = fixture.new_server()
            self.assertEqual(fixture.server.priority, changed)
            self.assertFalse(legacy.exists())
            fixture.restart()
            self.assertEqual(fixture.server.priority, changed)

    def test_import_converts_legacy_widths_and_calls_and_runs_only_once(self) -> None:
        import json
        for step, width, call, expected_width, expected_call in (
                ("nextest", 8, "legacy", gate.RequestedTestWidth(8), gate.CallRecord("legacy")),
                ("nextest", None, None, gate.UnspecifiedTestWidth(), gate.NoCallRecord()),
                ("check", None, None, gate.NoTestWidth(), gate.NoCallRecord())):
            with self.subTest(step=step, width=width), AdmissionFixture() as fixture:
                fixture.server.close()
                (fixture.state_dir / "state.json").unlink()
                owner = fixture.proc.spawn()
                legacy = fixture.state_dir / "legacy.reservation"
                row = {"pid": owner.pid, "start_time": owner.start_time, "need": GIB,
                       "repo": "fixture", "step": step, "worktree": "tree", "sidecar": "",
                       "admitted_at": gate.store.utc_iso(fixture.clock.now()),
                       "test_threads": width, "call_id": call}
                _ = legacy.write_text(json.dumps(row))
                fixture.server = fixture.new_server()
                record, = fixture.server.reservations.values()
                self.assertEqual(record.test_width, expected_width)
                self.assertEqual(record.call, expected_call)
                self.assertEqual(record.need, GIB)
                self.assertFalse(legacy.exists())
                before = service.read_state(fixture.state_dir)
                fixture.restart()
                self.assertEqual(service.read_state(fixture.state_dir), before)
                record, = fixture.server.reservations.values()
                self.assertEqual(record.test_width, expected_width)
                self.assertEqual(record.call, expected_call)
                _ = legacy.write_text("invalid legacy data must not be parsed")
                fixture.restart()
                self.assertEqual(service.read_state(fixture.state_dir), before)
                self.assertTrue(legacy.exists())

    def test_import_preserves_legacy_holder_names_without_a_queue_waiter(self) -> None:
        import json
        with AdmissionFixture() as fixture:
            fixture.server.close()
            (fixture.state_dir / "state.json").unlink()
            owner = fixture.proc.spawn()
            legacy = fixture.state_dir / "named.reservation"
            row = {"pid": owner.pid, "start_time": owner.start_time, "need": GIB,
                   "repo": "fixture", "step": "check", "worktree": "tree", "sidecar": "",
                   "admitted_at": gate.store.utc_iso(fixture.clock.now()),
                   "test_threads": None, "call_id": None,
                   "session_id": "legacy-session", "session_name": "legacy-holder"}
            _ = legacy.write_text(json.dumps(row))
            fixture.server = fixture.new_server()
            self.assertFalse(legacy.exists())
            for restarted in (False, True):
                with self.subTest(restarted=restarted):
                    if restarted:
                        fixture.restart()
                    record, = fixture.server.reservations.values()
                    self.assertEqual(record.queue_waiter.get("session_id"), "legacy-session")
                    self.assertEqual(record.queue_waiter.get("session_name"), "legacy-holder")
                    reply, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(reply, wire.ViewReply)
                    reservation, = reply.reading.explanation.reservations
                    assert "queue_waiter" in reservation
                    waiter = reservation["queue_waiter"]
                    self.assertEqual(waiter.get("session_id"), "legacy-session")
                    self.assertEqual(waiter.get("session_name"), "legacy-holder")

    def test_service_drain_reads_phase_five_state_with_launch_replay(self) -> None:
        from scripts.build_queue.admission_test_support import TestProcesses
        legacy = ('{"kind":"ServiceState","reservations":{},"places":{},'
                  '"ledger":{"6e44":{"kind":"ReplayEntry","reply":{"kind":"LaunchAdmitted",'
                  '"need":12884901888,"source":"shape"},"ended_at":1793601567.702956,"reservation":""}},'
                  '"recovery":{"kind":"NoRecoverySpacing"},"override":{"kind":"NoHeadAdmissionOverride"}}')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "state.json"
            _ = path.write_text(legacy)
            proc = TestProcesses(directory / "proc")
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                status = service.drain_check(directory, service=True, include_gate_callers=False, proc=proc)
            self.assertEqual(status, 0)
            self.assertEqual(output.getvalue(), "drained\n")
            state = service.read_state(directory)
            self.assertEqual(state.priority, gate.NO_QUEUE_PRIORITY)
            self.assertEqual(state.legacy_cleanup, ())
            self.assertEqual(state.ledger["6e44"], service.ReplayEntry(
                wire.LaunchAdmitted(12884901888, "shape"), 1793601567.702956))
            self.assertEqual(path.read_text(), legacy)

    def test_service_drain_rejects_unknown_state_fields(self) -> None:
        from scripts.build_queue.admission_test_support import TestProcesses
        state = service.ServiceState({}, {}, {}, gate.NoRecoverySpacing(), gate.NoHeadAdmissionOverride())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            proc = TestProcesses(directory / "proc")
            for legacy in (False, True):
                with self.subTest(legacy=legacy):
                    record = wire.parse_record(wire.encode_record(state), dict[str, object])
                    if legacy:
                        del record["priority"]
                        del record["legacy_cleanup"]
                    record["unknown"] = True
                    _ = (directory / "state.json").write_text(wire.encode_record(record))
                    with self.assertRaisesRegex(ValueError, "invalid snapshot record fields"):
                        _ = service.drain_check(directory, service=True, include_gate_callers=False, proc=proc)

    def service_drain_with_process(self, script: str, *, default_folder: bool) -> tuple[int, str]:
        from scripts.build_queue.admission_test_support import TestProcesses
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "admission"
            directory.mkdir()
            state = service.ServiceState({}, {}, {}, gate.NoRecoverySpacing(), gate.NoHeadAdmissionOverride())
            _ = (directory / "state.json").write_text(wire.encode_record(state))
            proc = TestProcesses(root / "proc")
            try:
                owner = proc.spawn()
                _ = (proc.path / str(owner.pid) / "cmdline").write_bytes(
                    f"python3\0/private/{script}\0".encode())
                argv = ["admission_service", "drain-check", "--service", "--state-dir", str(directory)]
                output = io.StringIO()
                with mock.patch.object(gate.store, "root", return_value=root if default_folder else root / "other"), \
                     mock.patch.object(service, "SystemProc", return_value=proc), \
                     mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", output):
                    status = service.main()
                return status, output.getvalue()
            finally:
                proc.close()

    def test_serve_without_state_dir_uses_the_buildlog_admission_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = mock.MagicMock()
            with mock.patch.object(gate.store, "root", return_value=root), \
                 mock.patch.object(service, "AdmissionServer", return_value=server) as made, \
                 mock.patch.object(sys, "argv", ["admission_service", "serve"]):
                self.assertEqual(service.main(), 0)
            self.assertEqual(made.call_args.args[0], root / "admission")

    def test_service_drain_other_folder_ignores_verify_process(self) -> None:
        status, output = self.service_drain_with_process("verify.sh", default_folder=False)
        self.assertEqual(status, 0)
        self.assertEqual(output, "drained\n")

    def test_service_drain_other_folder_counts_admission_client(self) -> None:
        status, output = self.service_drain_with_process("admission_client.py", default_folder=False)
        self.assertEqual(status, 1)
        self.assertIn("clients: 1:", output)
        self.assertIn("waiters: 0\nreservations: 0\n", output)

    def test_service_drain_default_folder_counts_verify_process(self) -> None:
        status, output = self.service_drain_with_process("verify.sh", default_folder=True)
        self.assertEqual(status, 1)
        self.assertIn("clients: 1:", output)
        self.assertIn("waiters: 0\nreservations: 0\n", output)

    def test_drain_barriers_count_live_waiters_reservations_and_client_processes(self) -> None:
        import json
        for rollback in (False, True):
            for kind in ("waiter", "reservation", "client", "empty"):
                with self.subTest(rollback=rollback, kind=kind), AdmissionFixture() as fixture:
                    request = fixture.request()
                    if kind == "waiter":
                        if rollback:
                            fixture.free(0)
                            _ = fixture.server.handle_request(request)
                        else:
                            _ = (fixture.state_dir / "old.waiting").write_text(json.dumps({
                                "pid": request.owner.pid, "start_time": request.owner.start_time,
                                "need": GIB, "since": fixture.clock.now()}))
                    elif kind == "reservation":
                        if rollback:
                            _ = fixture.server.handle_request(request)
                        else:
                            _ = (fixture.state_dir / "old.reservation").write_text(json.dumps({
                                "pid": request.owner.pid, "start_time": request.owner.start_time,
                                "need": GIB, "repo": "fixture", "step": "check", "worktree": "tree",
                                "admitted_at": gate.store.utc_iso(fixture.clock.now()),
                                "sidecar": "", "test_threads": None, "call_id": None}))
                    elif kind == "client":
                        script = "admission_client.py" if rollback else "verify.sh"
                        _ = (fixture.proc.path / str(request.owner.pid) / "cmdline").write_bytes(
                            f"python3\0/private/{script}\0".encode())
                    before = fixture.state_text()
                    output = io.StringIO()
                    with mock.patch("sys.stdout", output):
                        status = service.drain_check(fixture.state_dir, service=rollback,
                                                     include_gate_callers=True, proc=fixture.proc)
                    self.assertEqual(status, 0 if kind == "empty" else 1)
                    if kind == "empty":
                        self.assertEqual(output.getvalue(), "drained\n")
                    else:
                        label = {"waiter": "waiters", "reservation": "reservations", "client": "clients"}[kind]
                        self.assertIn(f"{label}: 1", output.getvalue())
                    self.assertEqual(fixture.state_text(), before)

    def test_estimate_jobs_keep_their_config_and_reject_an_old_two_four_two_revision(self) -> None:
        import threading
        with AdmissionFixture() as fixture:
            entered, resume = threading.Event(), threading.Event()
            margins: list[float] = []

            def need(row: gate.Reservation, _host: str, _now: object, _root: Path, *,
                     history_path: Path, config: gate.MemoryConfig) -> gate.TestPhaseNeed:
                self.assertEqual(history_path, fixture.state_dir / "anon_peaks.jsonl")
                margins.append(config.test_phase_margin)
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("the estimate worker did not resume")
                width = row["test_threads"]
                assert isinstance(width, gate.RequestedTestWidth)
                return gate.TestPhaseNeed(GIB, 7, width.threads, width.threads,
                                          "same_width", config.test_phase_margin)

            from scripts.lint.test_memory_admit import write_memory_config
            write_memory_config(fixture.root / "lint.conf", test_phase_margin="1.2")
            with mock.patch.object(gate, "test_phase_need", side_effect=need):
                grant = only_admitted(fixture.server.handle_request(fixture.request(width=gate.RequestedTestWidth(2))))
                self.assertTrue(entered.wait(5))
                record, = fixture.server.reservations.values()
                self.assertIsInstance(record.test_phase_need, gate.NoTestPhaseNeed)
                write_memory_config(fixture.root / "lint.conf", test_phase_margin="1.5")
                _ = fixture.server.handle_request(wire.WidthRequest("four", grant.reservation, 4))
                write_memory_config(fixture.root / "lint.conf", test_phase_margin="1.8")
                _ = fixture.server.handle_request(wire.WidthRequest("two-again", grant.reservation, 2))
                write_memory_config(fixture.root / "lint.conf", test_phase_margin="2.0")
                resume.set()
                fixture.server.resolve_estimates(3)
            self.assertEqual(margins, [1.2, 1.5, 1.8])
            record, = fixture.server.reservations.values()
            self.assertIsInstance(record.test_phase_need, gate.TestPhaseNeed)
            assert isinstance(record.test_phase_need, gate.TestPhaseNeed)
            self.assertEqual(record.test_phase_need.margin, 1.8)
            self.assertEqual(record.test_width, gate.RequestedTestWidth(2))
            self.assertEqual(service.read_state(fixture.state_dir).reservations, fixture.server.reservations)

    def test_a_slow_estimate_does_not_block_queries_or_release(self) -> None:
        import threading
        import time
        with AdmissionFixture() as fixture:
            entered, resume = threading.Event(), threading.Event()

            def delayed(*_args: object, **_kwargs: object) -> gate.TestPhaseNeed:
                entered.set()
                if not resume.wait(5):
                    raise AssertionError("the estimate worker did not resume")
                return gate.TestPhaseNeed(GIB, 5, 8, 8, "same_width", 1.2)

            with mock.patch.object(gate, "test_phase_need", side_effect=delayed):
                grant = only_admitted(fixture.server.handle_request(fixture.request()))
                self.assertTrue(entered.wait(5))
                try:
                    durations: list[float] = []
                    for _ in range(20):
                        started = time.monotonic()
                        reply = fixture.server.handle_request(wire.ViewRequest())[0]
                        durations.append(time.monotonic() - started)
                        self.assertIsInstance(reply, wire.ViewReply)
                    started = time.monotonic()
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0)), [wire.Ok()])
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertLess(max(durations), 1)
                    self.assertLess(sorted(durations)[18], 0.25)
                finally:
                    resume.set()
                fixture.server.resolve_estimates()
            self.assertEqual(fixture.server.reservations, {})
            self.assertEqual(service.read_state(fixture.state_dir).reservations, {})


    def phase_grant(self, fixture: AdmissionFixture, *, ramp: bool = False
                    ) -> tuple[wire.AdmitRequest, wire.Admitted, Path, gate.TestPhaseNeed]:
        need = gate.TestPhaseNeed(2 * GIB, 10, 8, 8, "same_width", 1.2)
        request = fixture.request(need=11 * GIB)
        request = replace(request, sidecar=str(fixture.scope(request.owner)))
        with mock.patch.object(gate, "test_phase_need", return_value=need):
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.server.resolve_estimates()
        record = fixture.server.reservations[request.episode]
        fixture.scoped_peak(grant, 11 * GIB)
        if not ramp:
            fixture.clock.advance(gate.read_memory_config().policy.timing.ramp_seconds + 1)
        phase = gate.test_phase_path(service.reservation_row(record))
        assert isinstance(phase, Path)
        fields: dict[str, object] = {"test_phase_peak_bytes": 3 * GIB,
            "test_phase_started_at": fixture.clock.now() - 30, "test_threads": 8,
            "call_id": "call-1", "test_scope": "full", "repo_identity": "fixture",
            "package": "", "filters": []}
        _ = phase.write_text(wire.encode_record(fields))
        return request, grant, phase, need

    def test_first_test_phase_charge_is_saved_once_and_copied_to_history(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture)
            _ = fixture.server.run_pass()
            record = fixture.server.reservations[request.episode]
            evidence = record.charge_use
            assert not isinstance(evidence, gate.NoTestPhaseChargeUse)
            expected = math.ceil(3 * GIB * need.margin)
            self.assertEqual((evidence["bytes"], evidence["selected_at"], evidence["reservation_id"]),
                             (expected, fixture.clock.now(), grant.reservation))
            self.assertEqual(gate.read_test_phase_need(evidence["need"]), need)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertEqual(gate.build_charge(inputs.running[0].measured), expected)
            fields = wire.parse_record(phase.read_text(), dict[str, object])
            fields["test_phase_peak_bytes"] = 5 * GIB
            _ = phase.write_text(wire.encode_record(fields))
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            self.assertEqual(fixture.server.reservations[request.episode].charge_use, evidence)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            memory, = inputs.running
            self.assertEqual(gate.build_charge(memory.measured), 6 * GIB)
            self.assertFalse(list(fixture.state_dir.glob("*.test-phase-charge.json")))
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual(row["test_phase_charge_use"], evidence)
            self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
            self.assertTrue(phase.exists())

    def test_saved_test_phase_margin_and_evidence_survive_finalization_crashes(self) -> None:
        for stage in ("after_finalization_intent", "after_history_append"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                request, grant, phase, need = self.phase_grant(fixture)
                _ = fixture.server.run_pass()
                evidence = fixture.server.reservations[request.episode].charge_use
                from scripts.build_queue.queue_fixture import write_memory_config
                write_memory_config(fixture.root / "lint.conf", test_phase_margin="2")
                inputs = fixture.server.last_inputs
                assert isinstance(inputs, gate.QueueInputs)
                memory, = inputs.running
                self.assertEqual(gate.build_charge(memory.measured), math.ceil(3 * GIB * need.margin))
                fixture.fault.stage = stage
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 137))
                phase.unlink()
                fixture.restart()
                _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
                row, = fixture.history()
                self.assertEqual((row["outcome"], row["test_phase_peak_bytes"]), ("killed", 3 * GIB))
                self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
                self.assertEqual(row["test_phase_charge_use"], evidence)
                self.assertFalse(fixture.server.reservations)

    def test_ramp_owner_exit_records_the_peak_without_inventing_charge_evidence(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture, ramp=True)
            _ = fixture.server.run_pass()
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            memory, = inputs.running
            self.assertEqual(gate.charge_basis(memory.measured), gate.RampPromise(11 * GIB))
            self.assertIsInstance(fixture.server.reservations[request.episode].charge_use, gate.NoTestPhaseChargeUse)
            fixture.proc.kill(request.owner)
            record = fixture.server.reservations[request.episode]
            scope = Path(Path(record.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.clock.advance(gate.read_memory_config().policy.timing.ramp_seconds + 1)
            fixture.server.run_once(0)
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["test_phase_peak_bytes"]), ("owner_gone", 3 * GIB))
            self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
            self.assertNotIn("test_phase_charge_use", row)
            self.assertTrue(phase.exists())
            self.assertFalse(fixture.server.reservations)
            self.assertFalse(Path(grant.max_path).exists())

    def test_old_sampler_history_keeps_the_need_without_test_phase_window_fields(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture)
            phase.unlink()
            _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            row, = fixture.history()
            self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
            for field in ("test_phase_peak_bytes", "test_phase_started_at", "test_phase_charge_use"):
                self.assertNotIn(field, row)
            self.assertNotIn(request.episode, fixture.server.reservations)

    def test_failed_need_worker_keeps_the_admission_and_replay_identity(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            with mock.patch.object(gate, "test_phase_need", side_effect=OSError("need reader failed")):
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.server.resolve_estimates()
            self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
            record, = fixture.server.reservations.values()
            self.assertEqual(record.reservation_id, grant.reservation)
            self.assertEqual(record.test_phase_need, gate.NoTestPhaseNeed("not_recorded"))


    def test_failed_optional_charge_write_keeps_the_reservation_and_selected_charge(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture)
            persist = fixture.server.persist
            failures: list[str] = []
            def reject_evidence(config: gate.MemoryConfig | None = None) -> None:
                record = fixture.server.reservations[request.episode]
                if not isinstance(record.charge_use, gate.NoTestPhaseChargeUse):
                    failures.append(record.reservation_id)
                    raise OSError("evidence disk unavailable")
                persist(config) if config is not None else persist()
            with mock.patch.object(fixture.server, "persist", side_effect=reject_evidence):
                _ = fixture.server.run_pass()
            self.assertEqual(failures, [grant.reservation])
            record = fixture.server.reservations[request.episode]
            self.assertIsInstance(record.charge_use, gate.NoTestPhaseChargeUse)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            self.assertEqual(gate.build_charge(inputs.running[0].measured), math.ceil(3 * GIB * need.margin))
            self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
            self.assertTrue(phase.exists())

    def test_priority_orders_new_workers_and_drops_rank_from_persisted_places(self) -> None:
        from scripts.message.unit_labels import UnitLabels
        with AdmissionFixture() as fixture:
            fixture.free(0)
            older = fixture.request(need=12 * GIB, since=fixture.clock.now() - 600)
            chosen = fixture.request(need=12 * GIB, since=fixture.clock.now() - 60)
            newest = fixture.request(need=12 * GIB, since=fixture.clock.now() - 30)
            labels = {older.identity.session_id: UnitLabels("older", unit="other"),
                chosen.identity.session_id: UnitLabels("chosen", unit="chosen"),
                newest.identity.session_id: UnitLabels("copy", unit="chosen")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                for request in (older, chosen):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.server.handle_request(wire.PrioritySetRequest("priority", "chosen", "user work", "director")), [wire.Ok()])
                _ = fixture.server.handle_request(newest)
                view = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [chosen.owner.pid, newest.owner.pid, older.owner.pid])
                state = service.read_state(fixture.state_dir)
                before = {key: (place.since, place.priority_since)
                    for key, place in state.places.items()}
                for waiting in fixture.server.waiters.values():
                    self.assertNotIn("priority_rank", gate.stored_waiter(waiting.row))
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest("clear", "director")), [wire.Ok()])
                view = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [older.owner.pid, chosen.owner.pid, newest.owner.pid])
                self.assertEqual({key: (place.since, place.priority_since)
                    for key, place in fixture.server.places.items()}, before)

    def test_recovery_slots_hold_later_waiters_and_expire_after_clear(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            requests = [fixture.request(need=GIB, since=fixture.clock.now() - age) for age in (300, 200, 100)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            with mock.patch.object(fixture.readings, "pressure", return_value=gate.MemoryPressure(6)):
                _ = fixture.server.run_pass()
            self.assertFalse(fixture.server.reservations)
            policy = fixture.server.last_policy
            fixture.free(60 * GIB)
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            self.assertEqual(list(fixture.server.reservations), [requests[0].episode])
            first = fixture.server.reservations[requests[0].episode]
            self.assertEqual(fixture.server.recovery,
                gate.RecoveryStarts(fixture.clock.now() + policy.timing.recovery_window_seconds,
                    fixture.clock.now() + policy.timing.recovery_start_seconds))
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-first", first.reservation_id, 0))
            fixture.clock.advance(policy.timing.recovery_start_seconds - 1)
            _ = fixture.server.run_pass()
            self.assertFalse(fixture.server.reservations)
            held = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(held, wire.ViewReply)
            head_reason = held.reading.view.waiting[0].reason
            assert isinstance(head_reason, gate.HeadAdmissionStatus)
            self.assertIsInstance(head_reason.decision.pressure, gate.MemoryPressure)
            self.assertEqual([step.reason for step in held.reading.view.waiting[1:]],
                [head_reason.decision.pressure] * (len(held.reading.view.waiting) - 1))
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            self.assertEqual(list(fixture.server.reservations), [requests[1].episode])
            fixture.clock.advance(policy.timing.recovery_window_seconds)
            _ = fixture.server.run_pass()
            self.assertIn(requests[2].episode, fixture.server.reservations)
            self.assertIsInstance(fixture.server.recovery, gate.NoRecoverySpacing)

    def test_estimate_expiry_keeps_recovery_place_and_basis_until_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request(age=fixture.server.last_policy.timing.head_seconds - 1)
            _ = fixture.server.handle_request(request)
            place = dict(fixture.server.waiters[request.episode].row)
            fixture.server.recovery = gate.RecoveryStarts(fixture.clock.now() + 120, fixture.clock.now() + 30)
            recovery = fixture.server.recovery
            fixture.clock.advance(2)
            _ = fixture.server.run_pass()
            held = fixture.server.waiters[request.episode].last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance(held.reason, gate.EstimateExpired)
            saved = fixture.server.waiters[request.episode].row
            for key in ("since", "priority_since", "need", "estimated_at", "need_source", "need_basis"):
                self.assertEqual(saved.get(key), place.get(key))
            self.assertEqual(fixture.server.recovery, recovery)
            self.assertFalse(fixture.server.reservations)
            refreshed = replace(request, profile=replace(request.profile,
                peak=gate.ExpectedPeak(12 * GIB, "measured", 9), estimated_at=fixture.clock.now()))
            _ = fixture.server.handle_request(refreshed)
            saved = fixture.server.waiters[request.episode].row
            self.assertEqual((saved["since"], saved["need"], saved.get("need_runs")), (request.since, 12 * GIB, 9))
            self.assertNotIsInstance(fixture.server.waiters[request.episode].last_reply, service.NoWaitReply)

    def test_policy_changes_reclassify_new_passes_without_changing_an_earlier_reply(self) -> None:
        from scripts.build_queue import queue_evidence
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB)
            grant = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            fixture.free(0)
            younger = fixture.request(need=10 * GIB)
            _ = fixture.server.handle_request(younger)
            old = fixture.server.handle_request(wire.ExplainRequest())[0]
            assert isinstance(old, wire.ExplainReply)
            self.assertIsInstance(old.explanation.running[0].basis, gate.RampPromise)
            old_release = gate.smallest_head_release(old.explanation)
            fixture.configure(ramp_seconds=1)
            fixture.clock.advance(2)
            _ = fixture.server.run_pass()
            current = fixture.server.handle_request(wire.ExplainRequest())[0]
            assert isinstance(current, wire.ExplainReply)
            self.assertNotIsInstance(current.explanation.running[0].basis, gate.RampPromise)
            self.assertEqual(old.explanation.policy.timing.ramp_seconds, 120)
            self.assertEqual(gate.smallest_head_release(old.explanation), old_release)
            self.assertEqual(fixture.server.reservations[request.episode].reservation_id, grant.reservation)
            self.assertEqual(record.need, 12 * GIB)
            self.assertEqual(queue_evidence.charge_text(old.explanation.running[0].basis,
                user_zone(), old.explanation.policy, "nextest"), "ramp promise: first 2 min after admission")


    def test_filtered_estimate_is_reused_and_release_keeps_repository_and_selection(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ["cargo", "nextest", "run", "--test-threads", "16", "-E", "package(hana) & test(native_rate)"]
            repository = gate.RepositoryIdentity("https://example.org/hana", "hana")
            peak = gate.ExpectedPeak(2 * GIB, "measured", 5)
            policy = gate.read_memory_config().policy
            from datetime import UTC, datetime
            with mock.patch.object(gate, "git_identity", return_value=(repository, "clone")) as identity, \
                 mock.patch.object(gate, "expected_peak", return_value=peak) as history:
                first = gate.step_estimate(argv, str(fixture.root), gate.InferFromArgv(),
                    datetime.fromtimestamp(fixture.clock.now(), UTC), fixture.state_dir.parent, policy)
                second = gate.step_estimate(argv, str(fixture.root), first.profile,
                    datetime.fromtimestamp(fixture.clock.now() + 1, UTC), fixture.state_dir.parent, policy)
            self.assertEqual((identity.call_count, history.call_count), (1, 1))
            self.assertEqual(history.call_args.kwargs["package"], "hana")
            self.assertEqual(history.call_args.kwargs["filters"], ("native_rate",))
            self.assertEqual(second.profile.peak, first.profile.peak)
            request = fixture.request(need=2 * GIB, width=gate.RequestedTestWidth(16))
            request = replace(request, argv=tuple(argv), profile=second.profile,
                sidecar=str(fixture.scope(request.owner)))
            fixture.free(0)
            held = fixture.server.handle_request(request)[0]
            self.assertIsInstance(held, wire.Wait)
            fixture.free(40 * GIB)
            _ = fixture.server.run_pass()
            grant = fixture.server.ledger[request.episode].reply
            assert isinstance(grant, wire.Admitted)
            fixture.scoped_peak(grant, GIB)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("filtered", grant.reservation, 0)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual((row["repo_identity"], row["test_scope"]), (repository.key, "filtered"))
            self.assertEqual((row["package"], row["filters"], row["test_threads"]), ("hana", ["native_rate"], 16))

    def test_token_wait_preserves_backfill_and_strict_windows_until_its_next_admission(self) -> None:
        with AdmissionFixture() as fixture:
            policy = gate.read_memory_config().policy
            head = fixture.request(need=12 * GIB,
                since=fixture.clock.now() - 2 * policy.timing.head_seconds)
            head = replace(head, sidecar=str(fixture.scope(head.owner)))
            grant = only_admitted(fixture.server.handle_request(head))
            priority_since = fixture.clock.now() - policy.timing.head_seconds
            fixture.server.reservations[head.episode] = replace(fixture.server.reservations[head.episode],
                queue_waiter={**fixture.server.reservations[head.episode].queue_waiter, "priority_since": priority_since})
            fixture.scoped_peak(grant)
            self.assertEqual(fixture.server.handle_request(wire.TokenWaitRequest("leave-token", grant.reservation)), [wire.Ok()])
            place, = fixture.server.places.values()
            self.assertEqual((place.since, place.priority_since, place.token_left_at),
                (head.since, priority_since, fixture.clock.now()))
            fixture.clock.advance(1)
            blocker = fixture.request(need=10 * GIB)
            blocking = only_admitted(fixture.server.handle_request(blocker))
            fixture.free(0)
            returned = replace(head, episode="returned", token_back_at=fixture.clock.now())
            _ = fixture.server.handle_request(returned)
            fixture.free(40 * GIB)
            young = fixture.request(need=4 * GIB)
            young_grant = only_admitted(fixture.server.handle_request(young))
            reply = fixture.server.waiters[returned.episode].last_reply
            assert isinstance(reply, wire.Wait) and isinstance(reply.reason, gate.HeadAdmissionStatus)
            self.assertIsInstance(reply.reason.backfill, gate.BackfillUntil)
            _ = fixture.server.handle_request(wire.ReleaseRequest("young-done", young_grant.reservation, 0))
            fixture.clock.advance(policy.timing.backfill_seconds)
            held = fixture.request(need=10 * GIB)
            reply = fixture.server.handle_request(held)[0]
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance(reply.reason, gate.BehindHead)
            _ = fixture.server.handle_request(wire.ReleaseRequest("blocker-done", blocking.reservation, 0))
            self.assertIn(returned.episode, fixture.server.reservations)
            self.assertNotIn(returned.episode, fixture.server.waiters)
            place, = [place for place in fixture.server.places.values() if place.owner == returned.owner]
            self.assertEqual(place.episode, returned.episode)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                gaps = [row for row in gate.queue_times_backward(stream) if row.get("kind") == "token_gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual((gaps[0]["pid"], gaps[0]["start_time"], gaps[0]["session_id"], gaps[0]["step"]),
                (head.owner.pid, head.owner.start_time, head.identity.session_id, "nextest"))
            self.assertLessEqual(float(str(gaps[0]["token_left_at"])), float(str(gaps[0]["token_back_at"])))

    def test_head_admission_removes_its_priority_window_before_the_next_step(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            head = fixture.request(need=2 * GIB,
                since=fixture.clock.now() - gate.read_memory_config().policy.timing.head_seconds)
            _ = fixture.server.handle_request(head)
            fixture.server.waiters[head.episode].row["priority_since"] = fixture.clock.now()
            fixture.free(40 * GIB)
            _ = fixture.server.run_pass()
            self.assertIn(head.episode, fixture.server.reservations)
            self.assertNotIn(head.episode, fixture.server.waiters)
            follower = fixture.request(need=GIB)
            _ = only_admitted(fixture.server.handle_request(follower))
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, gate.QueueInputs)
            result = fixture.server.last_result
            assert isinstance(result, gate.PassResult)
            self.assertIsInstance(result.rows[0].decision.head_room, gate.NoHeadRoom)

    def test_legacy_recovery_damage_warns_once_and_does_not_block_admission(self) -> None:
        import contextlib
        for contents in ('{"state":', '[]', '{"state":"recovering","ends_at":false}',
                         '{"state":"recovering","ends_at":NaN,"next_start_at":1}'):
            with self.subTest(contents=contents), AdmissionFixture() as fixture:
                fixture.server.close()
                (fixture.state_dir / "state.json").unlink()
                legacy = fixture.state_dir / "pressure-recovery"
                _ = legacy.write_text(contents)
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    fixture.server = fixture.new_server()
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn("pressure recovery record", lines[0])
                self.assertIn("ignored", lines[0])
                grant = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
                self.assertIn(grant.reservation, [record.reservation_id for record in fixture.server.reservations.values()])
                self.assertFalse(legacy.exists())


    def test_one_process_snapshot_covers_fresh_mature_and_reused_owners(self) -> None:
        with AdmissionFixture() as fixture:
            fresh = fixture.request(need=4 * GIB, owner=fixture.proc.spawn(GIB))
            _ = only_admitted(fixture.server.handle_request(fresh))
            fixture.clock.advance(gate.read_memory_config().policy.timing.ramp_seconds)
            mature = fresh
            fresh = fixture.request(need=4 * GIB, owner=fixture.proc.spawn(GIB // 2))
            _ = only_admitted(fixture.server.handle_request(fresh))
            reused = fixture.request(need=GIB)
            old = only_admitted(fixture.server.handle_request(reused))
            fixture.proc.fixture_owners[reused.owner.pid] = "reused-start"
            stat_path = fixture.proc.path / str(reused.owner.pid) / "stat"
            _ = stat_path.write_text(stat_path.read_text().replace(reused.owner.start_time, "reused-start"))
            with mock.patch.object(fixture.proc, "processes", wraps=fixture.proc.processes) as tables:
                result = fixture.server.run_pass()
            tables.assert_called_once_with()
            self.assertEqual((result.totals.promised, result.totals.measured), (7 * GIB // 2, 3 * GIB // 2))
            self.assertEqual({record.owner.pid for record in fixture.server.reservations.values()},
                {fresh.owner.pid, mature.owner.pid})
            self.assertFalse(Path(old.max_path).exists())

    def test_recovery_state_replace_keeps_the_previous_open_snapshot(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.recovery = gate.PressureHoldRecorded()
            fixture.server.persist()
            with (fixture.state_dir / "state.json").open() as previous:
                fixture.server.recovery = gate.RecoveryStarts(120, 30)
                fixture.server.persist()
                self.assertEqual(wire.parse_record(previous.read(), service.ServiceState).recovery,
                    gate.PressureHoldRecorded())
            self.assertEqual(service.read_state(fixture.state_dir).recovery, gate.RecoveryStarts(120, 30))

    def test_state_write_interruption_keeps_the_saved_width_for_restart_and_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(width=gate.RequestedTestWidth(8))
            grant = only_admitted(fixture.server.handle_request(request))
            before = fixture.state_text()
            mutation = wire.WidthRequest("new-width", grant.reservation, 2)
            replace_file = os.replace
            def stop(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                if Path(destination) == fixture.state_dir / "state.json":
                    raise OSError("state replace stopped")
                replace_file(source, destination)
            with mock.patch.object(os, "replace", side_effect=stop):
                with self.assertRaisesRegex(OSError, "state replace stopped"):
                    _ = fixture.server.handle_request(mutation)
            self.assertEqual(fixture.state_text(), before)
            fixture.restart()
            self.assertEqual(fixture.server.reservations[request.episode].test_width, gate.RequestedTestWidth(8))
            self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            fixture.restart()
            self.assertEqual(fixture.server.reservations[request.episode].test_width, gate.RequestedTestWidth(2))


class RunPlaceTests(unittest.TestCase):
    def reading(self, fixture: AdmissionFixture) -> gate.QueueRead:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return reply.reading

    def test_phase_ten_client_reconnect_after_restart_keeps_place_and_replays_one_grant(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            request = fixture.request()
            envelope = wire.parse_record(wire.encode_request(request), dict[str, object])
            data = cast(dict[str, object], envelope["data"])
            _ = data.pop("run", None)
            legacy = wire.encode_record(envelope) + "\n"
            peer = fixture.connect(legacy)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            peer.close()
            fixture.restart()
            fixture.clock.advance(1)
            other = fixture.request()
            _ = fixture.server.handle_request(other)
            peer = fixture.connect(legacy)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            self.assertEqual(fixture.queue_pids(), [request.owner.pid, other.owner.pid])
            self.assertEqual(fixture.server.waiters[request.episode].request, request)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            granted = only_admitted(peer.replies())
            peer.close()
            fixture.restart()
            replay = fixture.connect(legacy)
            self.assertEqual(only_admitted(replay.replies()), granted)
            self.assertEqual(len([record for record in fixture.server.reservations.values()
                                  if record.owner == request.owner]), 1)

    def test_run_order_never_moves_an_earlier_step_behind_its_own_arrival(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            peer = fixture.request(since=first.since - 10)
            _ = fixture.server.handle_request(peer)
            earlier = fixture.request(run=run, since=first.since - 20)
            _ = fixture.server.handle_request(earlier)
            self.assertEqual(fixture.queue_pids(), [earlier.owner.pid, peer.owner.pid])
            row, = (row for _, row in self.reading(fixture).ordered if row["pid"] == earlier.owner.pid)
            self.assertEqual(row["since"], earlier.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], earlier.since)

    def test_later_child_keeps_run_order_and_run_exit_restores_step_arrival(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.free(0)
            fixture.clock.advance(10)
            peers = [fixture.request()]
            _ = fixture.server.handle_request(peers[0])
            fixture.clock.advance(10)
            peers.append(fixture.request())
            _ = fixture.server.handle_request(peers[1])
            fixture.clock.advance(10)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, *(peer.owner.pid for peer in peers)])
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            self.assertEqual(row["since"], second.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            place = next(place for place in service.read_state(fixture.state_dir).places.values()
                         if place.owner == second.owner)
            self.assertEqual(place.since, second.since)
            self.assertNotEqual(first.owner, second.owner)
            fixture.end_run(run)
            self.assertEqual(fixture.queue_pids(), [*(peer.owner.pid for peer in peers), second.owner.pid])
            self.assertEqual(fixture.server.waiters[second.episode].row["since"], second.since)
            state = service.read_state(fixture.state_dir)
            self.assertFalse(state.run_places)
            restored = next(place for place in state.places.values() if place.owner == second.owner)
            self.assertEqual(restored.since, second.since)
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            assert "order_since" in row
            self.assertEqual(row["order_since"], second.since)
            self.assertFalse(fixture.server.reservations)

    def test_restart_between_steps_keeps_the_run_watch_without_a_memory_charge(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.clock.advance(2)
            state = service.read_state(fixture.state_dir)
            self.assertEqual(list(state.run_places.values()), [wire.RunPlace(run, first.since, first.owner)])
            self.assertFalse(state.reservations)
            self.assertFalse(state.places)
            fixture.restart()
            fixture.free(0)
            other = fixture.request()
            _ = fixture.server.handle_request(other)
            fixture.clock.advance(2)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, other.owner.pid])
            self.assertEqual(fixture.server.waiters[second.episode].row["since"], second.since)
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            fixture.end_run(run)
            self.assertEqual(fixture.queue_pids(), [other.owner.pid, second.owner.pid])
            self.assertFalse(service.read_state(fixture.state_dir).run_places)

    def test_run_exit_between_steps_removes_the_idle_watch_and_saved_place(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            self.assertTrue(service.read_state(fixture.state_dir).run_places)
            fixture.end_run(run)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            self.assertFalse(fixture.server.reservations)
            fixture.clock.advance(1)
            fixture.free(0)
            stale = fixture.request(run=run)
            _ = fixture.server.handle_request(stale)
            row, = (row for _, row in self.reading(fixture).ordered)
            assert "order_since" in row
            self.assertEqual(row["order_since"], stale.since)

    def test_child_episodes_and_reservations_stay_separate_under_one_run(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            fixture.free(0)
            first = fixture.request(run=run)
            _ = fixture.server.handle_request(first)
            fixture.clock.advance(1)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [first.owner.pid, second.owner.pid])
            for _, row in self.reading(fixture).ordered:
                assert "order_since" in row
                self.assertEqual(row["order_since"], first.since)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            one = only_admitted([fixture.server.ledger[first.episode].reply])
            two = only_admitted([fixture.server.ledger[second.episode].reply])
            self.assertNotEqual(first.episode, second.episode)
            self.assertNotEqual(one.reservation, two.reservation)
            self.assertEqual(set(fixture.server.reservations), {first.episode, second.episode})
            for request, granted in ((first, one), (second, two)):
                record = fixture.server.reservations[request.episode]
                self.assertEqual(record.owner, request.owner)
                self.assertEqual(record.queue_waiter["since"], request.since)
                replay = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(replay, granted)
            self.assertEqual(len(service.read_state(fixture.state_dir).run_places), 1)
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-one", one.reservation, 0))
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-two", two.reservation, 0))
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(len(service.read_state(fixture.state_dir).run_places), 1)

    def test_reconnect_and_token_reacquire_use_the_child_arrival_key(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.clock.advance(10)
            second = fixture.request(run=run)
            two = only_admitted(fixture.server.handle_request(second))
            _ = fixture.server.handle_request(wire.TokenWaitRequest("token-gap", two.reservation))
            fixture.free(0)
            fixture.clock.advance(1)
            again = replace(second, episode="child-after-token", token_back_at=fixture.clock.now())
            _ = fixture.server.handle_request(again)
            fixture.restart()
            _ = fixture.server.handle_request(again)
            _ = fixture.server.handle_request(again)
            places = [place for place in service.read_state(fixture.state_dir).places.values()
                      if place.owner == second.owner]
            self.assertEqual(len(places), 1)
            self.assertEqual(places[0].since, second.since)
            row, = (row for _, row in self.reading(fixture).ordered)
            self.assertEqual(row["since"], second.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                gaps = [row for row in gate.queue_times_backward(stream) if row.get("kind") == "token_gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["episode"], again.episode)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            later = only_admitted([fixture.server.ledger[again.episode].reply])
            self.assertNotEqual(later.reservation, two.reservation)

    def test_reused_run_pid_and_no_run_keep_each_steps_own_order(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.clock.advance(1)
            ordinary = fixture.request()
            _ = fixture.server.handle_request(ordinary)
            requests: list[wire.AdmitRequest] = []
            for identity in (wire.RunIdentity(run.pid, str(int(run.start_time) + 1)), wire.NoRun()):
                fixture.clock.advance(1)
                request = fixture.request(run=identity)
                requests.append(request)
                _ = fixture.server.handle_request(request)
                row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == request.owner.pid)
                assert "order_since" in row
                self.assertEqual(row["order_since"], request.since)
            self.assertEqual(fixture.queue_pids(), [ordinary.owner.pid, *(request.owner.pid for request in requests)])
            self.assertEqual(list(service.read_state(fixture.state_dir).run_places.values()),
                             [wire.RunPlace(run, first.since, first.owner)])

    def test_restart_drops_a_run_that_ended_while_the_service_was_stopped(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.server.handle_request(first)
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(1)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            fixture.server.close()
            fixture.proc.kill(wire.Owner(run.pid, run.start_time))
            fixture.server = fixture.new_server()
            for request in (second, peer, first):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [first.owner.pid, peer.owner.pid, second.owner.pid])
            self.assertFalse(service.read_state(fixture.state_dir).run_places)

    def test_run_types_round_trip_in_admit_and_service_state(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            run = fixture.run_owner()
            request = fixture.request(run=run)
            for identity in (run, wire.NoRun()):
                with self.subTest(identity=identity):
                    message = replace(request, run=identity)
                    self.assertEqual(wire.parse_request(wire.encode_request(message)), message)
            _ = fixture.server.handle_request(request)
            state = service.read_state(fixture.state_dir)
            self.assertEqual(list(state.run_places.values()), [wire.RunPlace(run, request.since, request.owner)])
            self.assertEqual(wire.parse_record(wire.encode_record(state), service.ServiceState), state)
            for value, schema in ((run, wire.RunIdentity), (wire.NoRun(), wire.NoRun),
                                  (wire.RunPlace(run, request.since, request.owner), wire.RunPlace)):
                self.assertEqual(gate.parse_snapshot_value(gate.snapshot_value(value), schema), value)
            fixture.restart()
            self.assertEqual(service.read_state(fixture.state_dir), state)

    def test_phase_ten_state_upgrade_preserves_pending_work_priority_and_replay(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("pending-release", grant.reservation, 137))
            saved = service.read_state(fixture.state_dir)
            self.assertNotIsInstance(saved.reservations[request.episode].finalization, service.NoFinalizationIntent)
            priority = gate.QueuePriority(unit="queue", why="the user", set_at=fixture.clock.now(), set_by="director")
            expected = replace(saved, priority=priority, build_history_generation=7)
            old = wire.parse_record(wire.encode_record(expected), dict[str, object])
            del old["run_places"]
            path = fixture.state_dir / "state.json"
            _ = path.write_text(wire.encode_record(old))
            before = path.read_text()
            restored = service.read_state(fixture.state_dir)
            self.assertEqual(restored, expected)
            self.assertEqual(path.read_text(), before)
            self.assertEqual(restored.ledger[request.episode].reply, grant)
            self.assertEqual(restored.reservations[request.episode].ending, saved.reservations[request.episode].ending)

    def test_display_age_and_aws_wait_threshold_use_child_arrival_not_run_order(self) -> None:
        from collections.abc import Callable
        from zoneinfo import ZoneInfo
        from scripts.aws_target import aws_target
        from scripts.aws_target.config import ControlSettings, read_control_settings
        from scripts.build_queue import head_facts
        from scripts.build_queue.queue_lines import plain
        from scripts.lib import conf

        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, since=fixture.clock.now() - 600)
            _ = fixture.finish_step(first)
            fixture.free(0)
            second = fixture.request(run=run, step="check")
            second = replace(second, profile=replace(second.profile,
                repository=gate.RepositoryIdentity("github.com/team/project", "project")))
            _ = fixture.server.handle_request(second)
            reading = self.reading(fixture)
            self.assertEqual(reading.view.waiting[0].since, second.since)
            model = queue_view.QueueModel(reading, {}, gate.NoWaiter(), head_facts.PaceUnavailable(), ZoneInfo("UTC"),
                queue_view.HostLoadUnavailable(), gate.smallest_head_release(reading.explanation))
            rendered = plain(queue_view.render_view(model, 100))
            row, = [line for line in rendered.splitlines() if "check" in line]
            self.assertEqual(row.split()[4], "0", rendered)
            settings_path = fixture.root / "aws_target.conf"
            _ = write_config_fixture(settings_path, "offload=on\nkinds=check\nstart_after_wait_s=300\n"
                                     + "region=us-east-1\ninstance_id=i-fixture\ncredentials_profile=fixture\n")
            configuration = conf.read(settings_path)
            settings = read_control_settings(configuration)
            controller = aws_target.Controller(settings_path, fixture.root / "aws", fixture.state_dir)
            eligible_steps = cast(Callable[[ControlSettings, int, conf.ConfigRead], int],
                                  getattr(controller, "_queued"))
            self.assertEqual(eligible_steps(settings, int(fixture.clock.now()), configuration), 0)
            fixture.clock.advance(300)
            _ = fixture.server.run_pass()
            self.assertEqual(eligible_steps(settings, int(fixture.clock.now()), configuration), 1)
