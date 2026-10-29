"""Hold intervals, missing events and coordination state on temporary records."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast, override
from unittest.mock import patch

import holds
import build_hold
import mac_test
from user_zone import user_zone
import index
import store
from test_index import Record, point_root_at
import tempfile

NOW = datetime(2026, 10, 12, 16, tzinfo=UTC)


class TestHoldEvent(TypedDict):
    kind: str
    host: str
    target: str
    event: str
    hold_key: str
    holder: str
    reason: str
    at: str
    until: str


HolderFile = TypedDict("HolderFile", {"holder": str, "since": str, "release_eta": str, "for": str})


def hold_event(event: Literal["began", "ended"], key: str, at: str,
               target: holds.HoldTarget = "natedev", host: str = "natedev",
               holder: str = "hana", reason: str = "render", until: str = "") -> Record:
    record: TestHoldEvent = {
        "kind": "hold_event", "host": host, "target": target, "event": event,
        "hold_key": key, "holder": holder, "reason": reason, "at": at, "until": until,
    }
    return dict(record)


def write_holder(key: str, since: datetime, name: str = "hana", reason: str = "render") -> None:
    directory = Path(os.environ["BUILD_HOLD_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    record: HolderFile = {"holder": name, "since": since.isoformat(), "release_eta": "unknown", "for": reason}
    _ = (directory / key).write_text(json.dumps(record))


def write_mac_block(since: datetime, holder: str = "hana", reason: str = "render") -> None:
    paths = mac_test.state_paths()
    paths.directory.mkdir(parents=True, exist_ok=True)
    block = mac_test.PendingMacBlock(
        holder, reason, since, since + timedelta(hours=48), mac_test.HolderQueue(holder),
        mac_test.NO_SHOWRUNNER, "still_on", mac_test.WaitingOnLocalRun("a test"),
    )
    mac_test.write_block(paths.block, block)


def timed_hold_events(start: datetime, now: datetime) -> list[Record]:
    rows: list[Record] = []
    host = store.host_name()
    for number in range(1_000):
        began = start + timedelta(seconds=number * 20)
        ended = began + timedelta(seconds=30)
        target: holds.HoldTarget = "mac" if number % 2 else "natedev"
        rows.extend((hold_event("began", f"timed-{number}", began.isoformat(), target, host),
                     hold_event("ended", f"timed-{number}", ended.isoformat(), target, host)))
    midnight = start.astimezone(user_zone()).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    for number in range(2):
        began = midnight - timedelta(minutes=10 + number)
        rows.extend((hold_event("began", f"cross-midnight-{number}", began.isoformat(), "natedev", host),
                     hold_event("ended", f"cross-midnight-{number}", (midnight + timedelta(minutes=10)).isoformat(), "natedev", host)))
    since = min(start + timedelta(minutes=15), now - timedelta(minutes=15))
    write_holder("timed-open", since)
    rows.append(hold_event("began", "timed-open", since.isoformat(), host=host))
    write_mac_block(since)
    rows.append(hold_event("began", since.isoformat(), since.isoformat(), "mac", host))
    rows.append(hold_event("began", "timed-missing-end", since.isoformat(), host=host))
    return rows


class HoldsTests(unittest.TestCase):
    root: Path = Path()
    connection: sqlite3.Connection  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        point_root_at(self, self.root / "buildlog")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": "UTC"})))
        self.connection = sqlite3.connect(":memory:")
        index.create_schema(self.connection)
        self.addCleanup(self.connection.close)

    def add(self, *rows: Record, src: str = "natedev/first.jsonl") -> None:
        for row in rows:
            index.add_hold_event(self.connection, row, src)

    def history(self) -> holds.HoldHistory:
        return holds.read_hold_history(self.connection, holds.read_current_holds(), NOW)

    def test_two_source_files_pair_by_key_even_when_the_holder_name_changes(self) -> None:
        self.add(hold_event("began", "session", "2026-10-12T12:00:00Z", holder="old-name"))
        self.add(hold_event("ended", "session", "2026-10-12T13:00:00Z", holder="new-name"), src="natedev/second.jsonl")
        self.assertEqual(cast(list[tuple[object, ...]], self.connection.execute(
            "SELECT holder, began_at, ended_at, later_row FROM holds",
        ).fetchall()), [("old-name", "2026-10-12T12:00:00Z", "2026-10-12T13:00:00Z", 1)])
        index.forget(self.connection, "natedev/first.jsonl")
        self.assertEqual(cast(list[tuple[object, ...]], self.connection.execute(
            "SELECT holder, began_at, ended_at FROM holds",
        ).fetchall()), [("new-name", None, "2026-10-12T13:00:00Z")])
        self.assertEqual(holds.coverage_lines(self.history())[-1],
                         "natedev holds recorded since 2026-10-12 13:00 UTC: 1 intervals, 0 end not recorded, 1 began not recorded")

    def test_forgetting_the_end_source_leaves_the_beginning_whole(self) -> None:
        self.add(hold_event("began", "session", "2026-10-12T12:00:00Z"))
        self.add(hold_event("ended", "session", "2026-10-12T13:00:00Z"), src="natedev/second.jsonl")
        index.forget(self.connection, "natedev/second.jsonl")
        interval = self.history().intervals[0]
        self.assertEqual(interval.began, datetime(2026, 10, 12, 12, tzinfo=UTC))
        self.assertIsInstance(interval.ended, holds.HoldEndNotRecorded)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM hold_events").fetchone(), (1,))

    def test_equal_instants_order_the_beginning_before_the_end(self) -> None:
        self.add(hold_event("ended", "session", "2026-10-12T12:00:00Z"),
                 hold_event("began", "session", "2026-10-12T12:00:00Z"))
        intervals = self.history().intervals
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0].began, intervals[0].ended)

    def test_a_missing_end_adds_no_time_until_its_key_is_in_the_coordination_record(self) -> None:
        since = datetime(2026, 10, 12, 12, tzinfo=UTC)
        self.add(hold_event("began", "session", since.isoformat(), host=store.host_name()))
        history = self.history()
        self.assertIsInstance(history.intervals[0].ended, holds.HoldEndNotRecorded)
        self.assertEqual(holds.hold_duration(history, "natedev", "2026-10-12T12:00:00Z", NOW.isoformat()), holds.HoldDuration(0, True))
        self.assertIn("end not recorded", holds.interval_lines(history, since.isoformat(), NOW.isoformat())[0])
        write_holder("session", since)
        history = self.history()
        self.assertEqual(history.intervals[0].ended, holds.OpenHold(NOW))
        self.assertEqual(holds.hold_duration(history, "natedev", since.isoformat(), NOW.isoformat()), holds.HoldDuration(14400, False))

    def test_another_beginning_of_a_held_key_keeps_the_earlier_interval_incomplete(self) -> None:
        first, second = datetime(2026, 10, 12, 12, tzinfo=UTC), datetime(2026, 10, 12, 14, tzinfo=UTC)
        self.add(hold_event("began", "session", first.isoformat(), host=store.host_name()),
                 hold_event("began", "session", second.isoformat(), host=store.host_name()))
        write_holder("session", second)
        intervals = self.history().intervals
        self.assertIsInstance(intervals[0].ended, holds.HoldEndNotRecorded)
        self.assertEqual(intervals[1].ended, holds.OpenHold(NOW))
        self.assertEqual(holds.hold_duration(self.history(), "natedev", first.isoformat(), NOW.isoformat()), holds.HoldDuration(7200, True))

    def test_a_remote_host_beginning_cannot_be_open_from_a_local_holder_file(self) -> None:
        since = datetime(2026, 10, 12, 12, tzinfo=UTC)
        self.add(hold_event("began", "session", since.isoformat(), host="other-controller"))
        write_holder("session", since)
        self.assertIsInstance(self.history().intervals[0].ended, holds.HoldEndNotRecorded)

    def test_two_holders_at_once_and_cross_midnight_count_only_the_union_in_the_day(self) -> None:
        self.add(hold_event("began", "a", "2026-10-11T23:00:00Z"),
                 hold_event("ended", "a", "2026-10-12T02:00:00Z"),
                 hold_event("began", "b", "2026-10-12T01:00:00Z"),
                 hold_event("ended", "b", "2026-10-12T03:00:00Z"))
        duration = holds.hold_duration(self.history(), "natedev", "2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z")
        self.assertEqual(duration, holds.HoldDuration(10800, False))
        self.assertEqual(holds.duration_text(duration), "3:00")

    def test_an_end_without_a_beginning_adds_no_time_and_marks_the_day_as_at_least(self) -> None:
        self.add(hold_event("ended", "a", "2026-10-12T12:00:00Z"))
        history = self.history()
        self.assertIsInstance(history.intervals[0].began, holds.HoldBeginningNotRecorded)
        self.assertEqual(holds.duration_text(holds.hold_duration(history, "natedev", "2026-10-12T00:00:00Z", NOW.isoformat())), "at least 0:00")
        self.assertIn("began not recorded", holds.interval_lines(history, "2026-10-12T00:00:00Z", NOW.isoformat())[0])

    def test_a_missing_beginning_marks_each_day_since_the_targets_first_event(self) -> None:
        self.add(hold_event("began", "other", "2026-10-09T00:00:00Z"),
                 hold_event("ended", "other", "2026-10-09T01:00:00Z"),
                 hold_event("ended", "missing", "2026-10-12T12:00:00Z"))
        history = self.history()
        for day in (10, 11, 12):
            with self.subTest(day=day):
                start = datetime(2026, 10, day, tzinfo=UTC)
                end = start + timedelta(days=1)
                self.assertEqual(holds.hold_duration(history, "natedev", start.isoformat(), end.isoformat()),
                                 holds.HoldDuration(0, True))
                self.assertIn("began not recorded", "\n".join(holds.interval_lines(history, start.isoformat(), end.isoformat())))
        self.assertEqual(holds.hold_duration(history, "natedev", "2026-10-13T00:00:00Z", "2026-10-14T00:00:00Z"),
                         holds.HoldDuration(0, False))

    def test_a_missing_beginning_cannot_precede_its_keys_previous_end(self) -> None:
        self.add(hold_event("began", "other", "2026-10-08T00:00:00Z"),
                 hold_event("ended", "other", "2026-10-08T01:00:00Z"),
                 hold_event("began", "session", "2026-10-10T00:00:00Z"),
                 hold_event("ended", "session", "2026-10-10T01:00:00Z"),
                 hold_event("ended", "session", "2026-10-12T12:00:00Z"))
        history = self.history()
        self.assertEqual(holds.hold_duration(history, "natedev", "2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z"),
                         holds.HoldDuration(0, False))
        self.assertEqual(holds.interval_lines(history, "2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z"), [])
        self.assertEqual(holds.hold_duration(history, "natedev", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z"),
                         holds.HoldDuration(0, True))

    def test_a_later_beginning_bounds_the_days_and_windows_of_a_missing_end(self) -> None:
        self.add(hold_event("began", "session", "2026-10-09T00:00:00Z"),
                 hold_event("began", "session", "2026-10-11T00:00:00Z"),
                 hold_event("ended", "session", "2026-10-11T01:00:00Z"))
        history = self.history()
        self.assertEqual(holds.hold_duration(history, "natedev", "2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z"),
                         holds.HoldDuration(0, True))
        self.assertEqual(holds.hold_duration(history, "natedev", "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z"),
                         holds.HoldDuration(3600, False))
        self.assertNotIn("end not recorded", "\n".join(holds.interval_lines(history, "2026-10-11T00:00:00Z", "2026-10-12T00:00:00Z")))
        self.assertEqual(holds.interval_lines(history, "2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z"), [])

    def test_invalid_planned_end_in_an_old_event_does_not_stop_the_report(self) -> None:
        self.add(hold_event("began", "session", "2026-10-12T12:00:00Z", until="later"),
                 hold_event("began", "naive", "2026-10-12T12:00:00Z", until="2026-10-12T14:00:00"))
        self.assertNotIn("planned end", "\n".join(holds.interval_lines(self.history(), "2026-10-12T00:00:00Z", NOW.isoformat())))

    def test_recording_starts_and_planned_ends_have_named_states(self) -> None:
        self.add(hold_event("began", "planned", "2026-10-12T12:00:00Z", until="2026-10-12T14:00:00Z"),
                 hold_event("began", "unknown", "2026-10-12T12:00:00Z"))
        history = self.history()
        self.assertEqual(history.recorded_since["mac"], holds.NoHoldEventRecorded.VALUE)
        self.assertEqual(history.recorded_since["natedev"], holds.RecordedSince(datetime(2026, 10, 12, 12, tzinfo=UTC)))
        self.assertEqual(history.intervals[0].planned_end, holds.PlannedEnd(datetime(2026, 10, 12, 14, tzinfo=UTC)))
        self.assertEqual(history.intervals[1].planned_end, holds.NoPlannedEnd.VALUE)
        self.assertIn("planned end 2026-10-12 14:00 UTC", "\n".join(holds.interval_lines(history, "2026-10-12T00:00:00Z", NOW.isoformat())))

    def test_the_timed_events_include_a_missing_end_without_a_current_hold(self) -> None:
        rows = timed_hold_events(NOW - timedelta(hours=20), NOW)
        self.add(*rows)
        history = self.history()
        missing = next(interval for interval in history.intervals if interval.key == "timed-missing-end")
        self.assertIsInstance(missing.ended, holds.HoldEndNotRecorded)
        self.assertFalse(holds.key_is_present(holds.read_current_holds(), missing.host, missing.target, missing.key))

    def test_present_holder_stays_held_and_open_despite_recorded_release(self) -> None:
        since = datetime(2026, 10, 12, 12, tzinfo=UTC)
        write_holder("session", since)
        cycle = build_hold.open_cycle(since)
        cycle["holders"] = {"session": {"since": since.isoformat(), "released_at": since.isoformat()}}
        cycle["release_started_at"] = since.isoformat()
        build_hold.save_cycle(cycle)
        self.add(hold_event("began", "session", since.isoformat(), host=store.host_name()))
        current = holds.read_current_holds()
        self.assertEqual(holds.now_lines(current), ["Now: natedev held by hana for render since 2026-10-12 12:00 UTC"])
        history = holds.read_hold_history(self.connection, current, NOW)
        self.assertEqual(history.intervals[0].ended, holds.OpenHold(NOW))
        self.assertEqual(holds.hold_duration(history, "natedev", since.isoformat(), NOW.isoformat()).seconds, 14400)
        (Path(os.environ["BUILD_HOLD_DIR"]) / "session").unlink()
        current = holds.read_current_holds()
        self.assertEqual(holds.now_lines(current), [])
        self.assertFalse(holds.key_is_present(current, store.host_name(), "natedev", "session"))

    def test_damaged_release_record_cannot_hide_a_present_holder(self) -> None:
        write_holder("session", NOW)
        directory = Path(os.environ["BUILD_HOLD_RELEASE_DIR"])
        directory.mkdir(parents=True)
        _ = (directory / "current").write_text("bad cycle id")
        lines = holds.now_lines(holds.read_current_holds())
        self.assertEqual(lines, ["Now: natedev held by hana for render since 2026-10-12 16:00 UTC"])
        (Path(os.environ["BUILD_HOLD_DIR"]) / "session").unlink()
        self.assertEqual(holds.now_lines(holds.read_current_holds()), [])

    def test_a_pending_mac_block_is_open_by_its_since_key_and_survives_its_planned_expiry(self) -> None:
        since = NOW - timedelta(days=3)
        write_mac_block(since)
        self.add(hold_event("began", since.isoformat(), since.isoformat(), "mac", store.host_name()))
        self.assertEqual(self.history().intervals[0].ended, holds.OpenHold(NOW))
        self.assertEqual(holds.now_lines(holds.read_current_holds()), ["Now: Mac blocked by hana for render since 2026-10-09 16:00 UTC"])

    def test_each_target_has_a_coverage_line_and_does_not_claim_current_state_from_events(self) -> None:
        self.add(hold_event("began", "mac-key", "2026-10-12T12:00:00Z", "mac"),
                 hold_event("ended", "mac-key", "2026-10-12T13:00:00Z", "mac"))
        self.assertEqual(holds.now_lines(holds.read_current_holds()), [])
        self.assertEqual(holds.coverage_lines(self.history()), [
            "mac holds recorded since 2026-10-12 12:00 UTC: 1 intervals, 0 end not recorded, 0 began not recorded",
            "natedev holds: no event recorded yet",
        ])

    def test_shown_times_use_the_user_zone_when_the_machine_zone_differs(self) -> None:
        write_holder("session", NOW)
        with patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": "America/New_York"}):
            self.assertIn("since 2026-10-12 12:00 EDT", holds.now_lines(holds.read_current_holds())[0])

    def test_hold_history_reads_the_view_and_coverage_once(self) -> None:
        self.add(hold_event("began", "session", "2026-10-12T12:00:00Z"))
        statements: list[str] = []
        self.connection.set_trace_callback(statements.append)
        _ = self.history()
        self.assertEqual(len(statements), 2)
        self.assertIn("FROM holds", statements[0])
        self.assertIn("FROM hold_events", statements[1])


if __name__ == "__main__":
    _ = unittest.main()
