from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from scripts.build_queue import target_availability
from scripts.build_queue.admission_records import PackageNotRecorded
from scripts.build_targets import build_targets, targets
from scripts.build_targets.target_config import ConfigError, ExcludedTarget, LocalTarget, SshTarget
from scripts.mac_test import offload
from scripts.message import unit_labels

MAC = SshTarget("mac", "macos", Path("/fixture/mac.conf"))
NATEDEV = LocalTarget("natedev", "linux")
SINCE = datetime(2026, 11, 13, 13, 50, tzinfo=UTC)
LATER = datetime(2026, 11, 13, 15, 10, tzinfo=UTC)
LABELS = {
    "mac-session": unit_labels.UnitLabels("research-director", "hana", "research"),
    "first-session": unit_labels.UnitLabels("report-director", "builds", "report"),
}
UNTIL_RELEASED = targets.UntilReleased()


def held(holder: str, session: str, *, since: datetime | targets.StartNotRecorded = SINCE,
         end: targets.HoldEnd = UNTIL_RELEASED, reason: str = "fixture reason") -> targets.Held:
    return targets.Held(holder, reason, since, end, targets.HolderSession(session))


def snapshot(target: SshTarget | LocalTarget, *states: targets.TargetState,
             backoff: offload.BackoffState = offload.NO_BACKOFF) -> targets.TargetSnapshot:
    return targets.TargetSnapshot(target, states, (), backoff)


MAC_BLOCK = held("research", "mac-session", reason="Mac CI restore",
                 end=targets.AwaitingBlockRemoval(LATER))
NATEDEV_HOLDS = (held("report", "first-session", end=targets.ReleaseEstimated(LATER), reason="disk walk"),
                 held("unnamed", "other-session", since=targets.StartNotRecorded(), reason="cargo sweep"))


class AvailabilityRowTests(unittest.TestCase):
    def test_mac_block_and_two_natedev_holds_name_owner_reason_and_times(self) -> None:
        snapshots = (snapshot(MAC, MAC_BLOCK), snapshot(NATEDEV, *NATEDEV_HOLDS))
        rows = target_availability.availability_rows(snapshots, LABELS, UTC)
        self.assertEqual(rows, [
            {"target": "mac", "state": "Blocked", "reason": "Mac CI restore", "holder": "research", "unit": "research",
             "showrunner": "hana", "since": "since 13:50 UTC",
             "until": "awaiting unblock; requested expiry was 15:10 UTC"},
            {"target": "natedev", "state": "Held", "reason": "disk walk", "holder": "report", "unit": "report",
             "showrunner": "builds", "since": "since 13:50 UTC", "until": "expected release 15:10 UTC"},
            {"target": "natedev", "state": "Held", "reason": "cargo sweep", "holder": "unnamed", "unit": "—",
             "showrunner": "—", "since": "start not recorded", "until": "until released"},
        ])
        self.assertEqual(target_availability.holder_sessions(snapshots),
                         ["mac-session", "first-session", "other-session"])

    def test_free_and_busy_targets_have_no_row(self) -> None:
        job = targets.RunningJob("nextest hana", SINCE, targets.NotSeenIdle())
        snapshots = (snapshot(MAC, targets.Running((job,))), snapshot(NATEDEV, targets.Free()))
        self.assertEqual(target_availability.availability_rows(snapshots, LABELS, UTC), [])

    def test_unreachable_and_unreadable_targets_show_as_unavailable(self) -> None:
        snapshots = (snapshot(MAC, targets.Unreachable("retry backoff")),
                     snapshot(NATEDEV, targets.StateUnreadable("holder file: bad json")))
        rows = target_availability.availability_rows(snapshots, LABELS, UTC)
        self.assertEqual([(row["target"], row["state"], row["reason"]) for row in rows],
                         [("mac", "Unavailable", "retry backoff"), ("natedev", "Unavailable", "holder file: bad json")])

    def test_an_unreadable_configuration_shows_one_row_and_no_sessions(self) -> None:
        unreadable = target_availability.SnapshotsUnreadable("targets.conf: missing")
        self.assertEqual(target_availability.availability_rows(unreadable, LABELS, UTC), [
            {"target": "targets", "state": "Unavailable", "reason": "targets.conf: missing", "holder": "", "unit": "",
             "showrunner": "", "since": "", "until": ""}])
        self.assertEqual(target_availability.holder_sessions(unreadable), [])
        self.assertEqual(target_availability.mac_claims(unreadable), ())

    def test_read_snapshots_returns_the_configuration_error(self) -> None:
        with mock.patch.object(targets, "target_snapshots", side_effect=OSError("disk gone")):
            self.assertEqual(target_availability.read_snapshots(), target_availability.SnapshotsUnreadable("disk gone"))
        with mock.patch.object(targets, "target_snapshots", side_effect=ConfigError("bad key")):
            self.assertIsInstance(target_availability.read_snapshots(), target_availability.SnapshotsUnreadable)


class MacRouteTests(unittest.TestCase):
    def routes(self, *mac_states: targets.TargetState) -> target_availability.MacRoutes:
        return target_availability.MacRoutes((snapshot(MAC, *mac_states), snapshot(NATEDEV, targets.Free())), LABELS, UTC)

    def test_each_mac_state_gives_its_line(self) -> None:
        job = targets.RunningJob("nextest hana", SINCE, targets.NotSeenIdle())
        cases: tuple[tuple[targets.TargetState, str], ...] = (
            (MAC_BLOCK, "Mac: blocked by research (hana) awaiting unblock; requested expiry was 15:10 UTC"),
            (held("solo", "unknown-session", end=targets.ExpiresAt(LATER)), "Mac: blocked by solo until 15:10 UTC"),
            (targets.Running((job, job)), "Mac: busy with nextest hana; nextest hana"),
            (targets.Unreachable("probe failed"), "Mac: unreachable, probe failed"),
            (targets.StateUnreadable("block.json: bad"), "Mac: state unreadable, block.json: bad"),
            (targets.Free(), "Mac: no recorded block or claim; availability not checked"),
        )
        for state, expected in cases:
            with self.subTest(expected=expected), \
                 mock.patch.object(build_targets, "step_policy", return_value=offload.KnownRepository("hana", "/r")):
                routes = self.routes(state)
                self.assertEqual(routes.line("nextest", "/fixture/hana", "hana"), expected)
                self.assertEqual(routes.blocked(), isinstance(state, targets.Held))

    def test_kind_and_routing_rule_come_before_target_state(self) -> None:
        routes = self.routes(MAC_BLOCK)
        with mock.patch.object(build_targets, "step_policy", side_effect=AssertionError("no policy read")):
            self.assertEqual(routes.line("fmt", "/fixture/hana", "hana"), "Mac: kind not offloaded")
            self.assertEqual(routes.line("build", "/fixture/hana", "hana"), "Mac: natedev only, makes natedev artifacts")

    def test_policy_exclusion_names_its_reason_and_reads_once_for_each_key(self) -> None:
        routes = self.routes(targets.Free())
        excluded = ExcludedTarget(MAC, "linux_only")
        with mock.patch.object(build_targets, "step_policy", return_value=excluded) as policy:
            self.assertEqual(routes.line("nextest", "/fixture/hana", "hana"), "Mac: excluded, linux_only")
            self.assertEqual(routes.line("nextest", "/fixture/hana", "hana"), "Mac: excluded, linux_only")
            self.assertEqual(routes.line("nextest", "/fixture/hana", PackageNotRecorded()),
                             "Mac: excluded, linux_only; package not recorded")
            self.assertEqual(routes.line("nextest", "/fixture/hana", ""), "Mac: excluded, linux_only")
        self.assertEqual(policy.call_count, 3)

    def test_dynamic_exclusions_leave_the_state_line(self) -> None:
        for reason in sorted(target_availability.DYNAMIC_REASONS):
            with self.subTest(reason=reason), \
                 mock.patch.object(build_targets, "step_policy", return_value=ExcludedTarget(MAC, reason)):
                self.assertEqual(self.routes(targets.Unreachable("retry backoff")).line("check", "/fixture/hana", "hana"),
                                 "Mac: unreachable, retry backoff")

    def test_a_policy_read_error_shows_as_unreadable(self) -> None:
        with mock.patch.object(build_targets, "step_policy", side_effect=ConfigError("offload.conf: bad")):
            self.assertEqual(self.routes(targets.Free()).line("check", "/fixture/hana", "hana"),
                             "Mac: state unreadable, offload.conf: bad")

    def test_unreadable_or_absent_mac_target(self) -> None:
        unreadable = target_availability.MacRoutes(target_availability.SnapshotsUnreadable("targets.conf: missing"), {}, UTC)
        self.assertEqual(unreadable.line("nextest", "/r", "p"), "Mac: state unreadable, targets.conf: missing")
        self.assertFalse(unreadable.blocked())
        absent = target_availability.MacRoutes((snapshot(NATEDEV, targets.Free()),), {}, UTC)
        self.assertEqual(absent.line("nextest", "/r", "p"), "")
        self.assertFalse(absent.blocked())


if __name__ == "__main__":
    _ = unittest.main()
