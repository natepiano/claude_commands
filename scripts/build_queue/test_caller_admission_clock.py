"""Caller fixtures skip idle spacing without changing time or live caller admission."""
from __future__ import annotations

from dataclasses import replace
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, caller_admission, only_admitted
from scripts.lint import memory_admit as gate


def released_spacing(fixture: AdmissionFixture) -> float:
    """Release a standalone step and return its next start deadline."""
    grant = only_admitted(fixture.server.handle_request(
        fixture.request(need=GIB, owner=fixture.fixture_owner(1001, "first"))))
    fixture.release(grant)
    spacing = fixture.server.spacing
    assert isinstance(spacing, gate.NextStartAt)
    return spacing.at


class CallerAdmissionClockTests(unittest.TestCase):
    def caller(self, *, skip_idle_spacing: bool = True) -> AdmissionFixture:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {"BUILDLOG_DIR": str(root / "log")}
        fixture = caller_admission(self, root, environment, skip_idle_spacing=skip_idle_spacing)
        with fixture.loop_turn():
            fixture.configure(recovery_start_seconds=60)
        return fixture

    def fixture(self, *, skip_idle_spacing: bool = True) -> AdmissionFixture:
        fixture = self.enterContext(AdmissionFixture(skip_idle_spacing=skip_idle_spacing))
        fixture.configure(recovery_start_seconds=60)
        return fixture

    def assert_spacing_wait(self, reply: wire.Reply, deadline: float) -> None:
        self.assertIsInstance(reply, wire.Wait)
        assert isinstance(reply, wire.Wait)
        self.assertIsInstance(reply.reason, gate.HeadAdmissionStatus)
        assert isinstance(reply.reason, gate.HeadAdmissionStatus)
        self.assertEqual(reply.reason.cause, gate.StartSpacingWait(deadline))

    def test_idle_admit_skips_spacing_without_moving_the_clock(self) -> None:
        fixture = self.fixture()
        with fixture.loop_turn():
            deadline = released_spacing(fixture)
            before = fixture.clock.now()
            self.assertFalse(fixture.server.slots)
            request = fixture.request(need=GIB, owner=fixture.fixture_owner(1002, "second"))
            reply, = fixture.server.handle_request(request)
            self.assertIsInstance(reply, wire.Admitted)
            self.assertEqual(fixture.clock.now(), before)
            self.assertEqual(fixture.server.spacing, gate.NextStartAt(deadline))
            self.assertEqual(service.read_state(fixture.state_dir).spacing, fixture.server.spacing)

    def test_idle_launch_skips_spacing_without_moving_the_clock(self) -> None:
        fixture = self.fixture()
        with fixture.loop_turn():
            deadline = released_spacing(fixture)
            before = fixture.clock.now()
            request = fixture.request(need=GIB, owner=fixture.fixture_owner(1002, "launch"))
            launch = wire.LaunchRequest(request.episode, request.owner, request.since,
                request.identity, request.profile, code_version=request.code_version)
            reply, = fixture.server.handle_request(launch)
            self.assertIsInstance(reply, wire.LaunchAdmitted)
            self.assertEqual(fixture.clock.now(), before)
            self.assertEqual(fixture.server.spacing, gate.NextStartAt(deadline))

    def test_a_live_reservation_keeps_the_spacing(self) -> None:
        fixture = self.fixture()
        with fixture.loop_turn():
            first = fixture.request(need=GIB, owner=fixture.fixture_owner(1001, "first"))
            _ = only_admitted(fixture.server.handle_request(first))
            spacing = fixture.server.spacing
            assert isinstance(spacing, gate.NextStartAt)
            second = fixture.request(need=GIB, owner=fixture.fixture_owner(1002, "second"))
            reply, = fixture.server.handle_request(second)
            self.assert_spacing_wait(reply, spacing.at)
            self.assertLess(fixture.clock.now(), spacing.at)
            self.assertIn(first.episode, fixture.server.reservations)
            self.assertNotIn(second.episode, fixture.server.reservations)

    def test_a_live_waiter_keeps_the_spacing(self) -> None:
        fixture = self.fixture()
        with fixture.loop_turn():
            fixture.hold_starts()
            first = fixture.request(need=GIB, owner=fixture.fixture_owner(1001, "first"))
            self.assertIsInstance(fixture.server.handle_request(first)[0], wire.Wait)
            deadline = fixture.clock.now() + 60
            fixture.server.spacing = gate.NextStartAt(deadline)
            second = fixture.request(need=GIB, owner=fixture.fixture_owner(1002, "second"))
            self.assertIsInstance(fixture.server.handle_request(second)[0], wire.Wait)
            self.assertLess(fixture.clock.now(), deadline)
            self.assertEqual(fixture.server.spacing, gate.NextStartAt(deadline))
            self.assertIn(first.episode, fixture.server.waiters)
            self.assertIn(second.episode, fixture.server.waiters)

    def test_a_live_between_steps_slot_keeps_the_spacing(self) -> None:
        fixture = self.fixture()
        with fixture.loop_turn():
            run = fixture.run_owner()
            first = only_admitted(fixture.server.handle_request(fixture.request(
                need=GIB, run=run, owner=fixture.fixture_owner(1001, "first"))))
            fixture.release(first)
            slot = fixture.server.slots[first.reservation]
            held_run = gate.between_steps(slot).run
            self.assertEqual((held_run.pid, held_run.start_time), (run.pid, run.start_time))
            self.assertTrue(gate.slot_in_force(slot, fixture.clock.now()))
            self.assertFalse(fixture.caller_reservations())
            spacing = fixture.server.spacing
            assert isinstance(spacing, gate.NextStartAt)
            reply, = fixture.server.handle_request(fixture.request(
                need=GIB, owner=fixture.fixture_owner(1002, "second")))
            self.assert_spacing_wait(reply, spacing.at)
            self.assertLess(fixture.clock.now(), spacing.at)
            self.assertIn(first.reservation, fixture.server.slots)

    def test_scenery_reservations_and_slots_do_not_keep_the_spacing(self) -> None:
        fixture = self.fixture()
        with fixture.loop_turn():
            scenery = fixture.occupy_host()
            before = fixture.clock.now()
            fixture.server.spacing = gate.NextStartAt(before + 60)
            reply = only_admitted(fixture.server.handle_request(fixture.request(
                need=GIB, owner=fixture.fixture_owner(1001, "first"))))
            self.assertEqual(fixture.clock.now(), before)
            fixture.release(reply)
            fixture.release(scenery)
            self.assertIn(scenery.reservation, fixture.server.slots)
            next_reply, = fixture.server.handle_request(fixture.request(
                need=GIB, owner=fixture.fixture_owner(1002, "second")))
            self.assertIsInstance(next_reply, wire.Admitted)
            self.assertEqual(fixture.clock.now(), before)

    def test_the_opt_out_keeps_idle_spacing(self) -> None:
        fixture = self.fixture(skip_idle_spacing=False)
        with fixture.loop_turn():
            deadline = released_spacing(fixture)
            request = fixture.request(need=GIB, owner=fixture.fixture_owner(1002, "second"))
            reply, = fixture.server.handle_request(request)
            self.assert_spacing_wait(reply, deadline)
            self.assertLess(fixture.clock.now(), deadline)
            fixture.allow_next_start()
            _ = fixture.run_pass()
            self.assertIn(request.episode, fixture.server.reservations)

    def test_allow_next_start_acquires_the_loop_turn(self) -> None:
        fixture = self.fixture(skip_idle_spacing=False)
        with fixture.loop_turn():
            deadline = released_spacing(fixture)
        advance = fixture.clock.advance
        caller_thread = threading.get_ident()

        def advance_with_access(seconds: float) -> None:
            if threading.get_ident() == caller_thread:
                fixture.require_loop_turn("clock advance")
            advance(seconds)

        with fixture.serving(), mock.patch.object(fixture.clock, "advance", side_effect=advance_with_access):
            fixture.allow_next_start()
            with fixture.loop_turn():
                reply, = fixture.server.handle_request(fixture.request(
                    need=GIB, owner=fixture.fixture_owner(1002, "second")))
                self.assertIsInstance(reply, wire.Admitted)
                self.assertGreaterEqual(fixture.clock.now(), deadline)

    def test_independent_admissions_keep_real_estimates_fresh_and_spacing_durable(self) -> None:
        fixture = self.fixture()
        fixture.configure(recovery_start_seconds=5)
        fixture.clock.epoch = time.time()
        started = fixture.clock.now()
        real_started = time.monotonic()
        with fixture.loop_turn():
            for index in range(6):
                request = fixture.request(need=GIB, owner=fixture.fixture_owner(1001 + index, str(index)))
                request = replace(request, since=time.time(), fresh_test=True,
                                  profile=replace(request.profile, estimated_at=time.time()))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.release(grant)
                self.assertEqual(fixture.server.spacing, gate.NextStartAt(started + 5))
                self.assertEqual(service.read_state(fixture.state_dir).spacing, fixture.server.spacing)
            self.assertLess(fixture.clock.now() - started, time.monotonic() - real_started + 5)
            fixture.advance_idle_spacing()
            self.assertEqual(fixture.server.spacing, gate.ReadyToStart())
            _ = fixture.run_pass()
            self.assertEqual(service.read_state(fixture.state_dir).spacing, gate.ReadyToStart())

    def test_reusing_a_caller_keeps_the_opt_out_in_both_orders(self) -> None:
        for first_skip in (True, False):
            with self.subTest(first_skip=first_skip):
                fixture = self.caller(skip_idle_spacing=first_skip)
                environment = {"BUILDLOG_DIR": str(fixture.state_dir.parent),
                               "ADMISSION_SOCKET": str(fixture.socket_path)}
                reused = caller_admission(self, fixture.root, environment, skip_idle_spacing=not first_skip)
                self.assertIs(reused, fixture)
                with fixture.loop_turn():
                    deadline = released_spacing(fixture)
                    reply, = fixture.server.handle_request(fixture.request(
                        need=GIB, owner=fixture.fixture_owner(1002, "second")))
                    self.assert_spacing_wait(reply, deadline)
                    fixture.end_fixture_owner(1002)
                    self.assertFalse(fixture.server.waiters)
                reused = caller_admission(self, fixture.root, environment)
                self.assertIs(reused, fixture)
                with fixture.loop_turn():
                    reply, = fixture.server.handle_request(fixture.request(
                        need=GIB, owner=fixture.fixture_owner(1003, "third")))
                    self.assert_spacing_wait(reply, deadline)


if __name__ == "__main__":
    _ = unittest.main()
