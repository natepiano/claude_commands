"""Postmaster client outcomes with scratch files and a clock that never sleeps."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast, override
from unittest.mock import patch

import postmaster
import sessions

ENTRY_ID = "0123456789abcdef0123456789abcdef"
TEXT = "Message from Sender via postmaster: Question [01234567]\n\nQuestion"
EPOCH = 1_800_000_000.0


def session(identity: str, name: str) -> sessions.SessionRecord:
    return sessions.SessionRecord(pid=123, sessionId=identity, name=name,
        messagingSocketPath=f"/test-sockets/{identity}.sock", updatedAt=1, tmux="", formerNames=[])


class FakeClock:
    def __init__(self) -> None:
        self.elapsed: float = 0.0
        self.after_sleep: Callable[[], None] = lambda: None
        self.sleeps: list[float] = []

    def time(self) -> float:
        return EPOCH + self.elapsed

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds
        self.after_sleep()


class PostmasterTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.state: Path = Path()
        self.directory: Path = Path()
        self.clock: FakeClock = FakeClock()
        self.recipient: sessions.SessionRecord = session("recipient", "Recipient")
        self.sender: sessions.SessionRecord = session("sender", "Sender")
        self.worker: sessions.SessionRecord = session("worker", "Postmaster")
        self.live: list[sessions.SessionRecord] = []
        self.zone: timezone = timezone(timedelta(hours=9))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "message"
        self.directory = self.state / "postmaster"
        self.clock = FakeClock()
        self.recipient = session("recipient", "Recipient")
        self.sender = session("sender", "Sender")
        self.worker = session("worker", "Postmaster")
        self.live = [self.recipient, self.sender, self.worker]
        self.zone = timezone(timedelta(hours=9))
        _ = self.enterContext(patch.object(postmaster, "user_zone", return_value=self.zone))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root)})))
        _ = self.enterContext(patch.object(time, "time", self.clock.time))
        _ = self.enterContext(patch.object(time, "monotonic", self.clock.monotonic))
        _ = self.enterContext(patch.object(time, "sleep", self.clock.sleep))

    def path(self, folder: str) -> Path:
        return self.directory / folder / f"{ENTRY_ID}.json"

    def write(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(value))

    def heartbeat(self, *, age: float = 0, identity: str = "worker") -> None:
        self.write(self.directory / "heartbeat.json", postmaster.Heartbeat(
            session_id=identity, at_ms=int((self.clock.time() - age) * 1000), version="0.1.0", run="1800000000000-test-worker"))

    def result(self, outcome: Literal["delivered", "failed", "expired", "withdrawn", "abandoned"], *, matching: bool = True) -> None:
        self.write(self.path("results"), postmaster.OutboxResult(id=ENTRY_ID, outcome=outcome,
            detail="fake mod result", sha256=hashlib.sha256(TEXT.encode()).hexdigest() if matching else "0" * 64,
            claimed_ms=int(self.clock.time() * 1000), finished_ms=int(self.clock.time() * 1000)))

    def claim(self) -> None:
        self.write(self.path("claimed"), {"id": ENTRY_ID, "claimed_ms": int(self.clock.time() * 1000), "run": "fake-worker"})

    def attempt(self, sender: str = "Sender") -> postmaster.Attempt:
        return postmaster.attempt(self.state, self.recipient, sender, ENTRY_ID, TEXT, self.live)

    def no_id_files(self) -> None:
        self.assertEqual([path for path in self.directory.rglob("*") if ENTRY_ID in path.name], [])

    def respond_on_poll(self, outcome: Literal["delivered", "failed", "expired", "withdrawn", "abandoned"], *, matching: bool = True) -> None:
        def respond() -> None:
            if self.path("outbox").exists():
                self.claim()
                self.result(outcome, matching=matching)
        self.clock.after_sleep = respond

    def test_constants_and_entry_ids(self) -> None:
        self.assertEqual((postmaster.WAIT_SECONDS, postmaster.CLAIM_GRACE_SECONDS,
            postmaster.HEARTBEAT_MAX_AGE_SECONDS, postmaster.POLL_SECONDS), (3.0, 2.0, 2.0, 0.05))
        ids = [postmaster.new_entry_id() for _ in range(2)]
        self.assertNotEqual(ids[0], ids[1])
        for identity in ids:
            self.assertRegex(identity, r"^[a-f0-9]{32}$")

    def test_absent_heartbeat_reads_once_and_creates_no_file(self) -> None:
        original = Path.read_text
        reads: list[Path] = []
        def read(path: Path, *args: object, **kwargs: object) -> str:
            del args, kwargs
            reads.append(path)
            return original(path)
        with patch.object(Path, "read_text", read):
            answer = self.attempt()
        self.assertEqual(answer, postmaster.RelayRequired("absent", 0.0))
        self.assertEqual(reads, [self.directory / "heartbeat.json"])
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_stale_unknown_worker_and_self_return_without_entry(self) -> None:
        cases = [(3.0, "worker", "stale"), (0.0, "gone", "stale"), (0.0, "recipient", "self")]
        for age, identity, reason in cases:
            with self.subTest(reason=reason, identity=identity):
                self.heartbeat(age=age, identity=identity)
                answer = self.attempt()
                self.assertIsInstance(answer, postmaster.RelayRequired)
                assert isinstance(answer, postmaster.RelayRequired)
                self.assertEqual(answer.reason, reason)
                self.no_id_files()

    def test_heartbeat_without_worker_identity_is_stale_and_status_is_not_running(self) -> None:
        for run_fields in ({}, {"run": ""}, {"run": 17}):
            with self.subTest(run_fields=run_fields):
                self.write(self.directory / "heartbeat.json", {
                    "session_id": "worker", "at_ms": int(self.clock.time() * 1000),
                    "version": "0.1.0", **run_fields,
                })
                self.assertEqual(self.attempt(), postmaster.RelayRequired("stale", 0.0))
                self.no_id_files()
                with patch.object(sessions, "live_sessions", return_value=self.live), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(postmaster.status(self.state), 1)
                self.assertIn("postmaster: not running;", output.getvalue())

    def test_status_prints_the_current_worker_identity(self) -> None:
        self.heartbeat()
        with patch.object(sessions, "live_sessions", return_value=self.live), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(postmaster.status(self.state), 0)
        self.assertIn("running, heartbeat 0.0 s old, worker 1800000000000-test-worker;", output.getvalue())

    def test_shutdown_sender_reads_and_writes_nothing_even_with_fresh_heartbeat(self) -> None:
        self.heartbeat()
        with patch.object(Path, "read_text", side_effect=AssertionError("relay-only read")), patch.object(Path, "write_text", side_effect=AssertionError("relay-only write")):
            self.assertEqual(self.attempt("shutdown"), postmaster.RelayRequired("relay-only-sender", 0.0))
        self.no_id_files()

    def test_torn_heartbeat_is_read_again_and_persistent_bad_json_is_stale(self) -> None:
        self.heartbeat()
        original = Path.read_text
        reads = 0
        def read(path: Path, *args: object, **kwargs: object) -> str:
            nonlocal reads
            del args, kwargs
            if path == self.directory / "heartbeat.json":
                reads += 1
                if reads == 1:
                    return "{"
            return original(path)
        self.respond_on_poll("delivered")
        with patch.object(Path, "read_text", read):
            self.assertIsInstance(self.attempt(), postmaster.Delivered)
        self.assertEqual(reads, 2)
        self.assertEqual(self.clock.sleeps[0], 0.02)
        _ = (self.directory / "heartbeat.json").write_text("{")
        self.clock.sleeps.clear()
        self.assertIsInstance(answer := self.attempt(), postmaster.RelayRequired)
        assert isinstance(answer, postmaster.RelayRequired)
        self.assertEqual(answer.reason, "stale")
        self.assertEqual(self.clock.sleeps, [0.02, 0.02])

    def test_results_choose_delivery_or_relay_and_remove_every_id_file(self) -> None:
        cases: list[tuple[Literal["delivered", "failed", "expired", "withdrawn", "abandoned"], bool, str]] = [
            ("delivered", True, "delivered"), ("delivered", False, "delivered"),
            ("failed", True, "failed"), ("expired", True, "expired"),
            ("abandoned", True, "claimed-no-result")]
        for outcome, matching, expected in cases:
            with self.subTest(outcome=outcome, matching=matching):
                self.heartbeat()
                self.respond_on_poll(outcome, matching=matching)
                answer = self.attempt()
                if expected == "delivered":
                    self.assertIsInstance(answer, postmaster.Delivered)
                    assert isinstance(answer, postmaster.Delivered)
                    self.assertEqual(answer.verbatim, matching)
                else:
                    self.assertIsInstance(answer, postmaster.RelayRequired)
                    assert isinstance(answer, postmaster.RelayRequired)
                    self.assertEqual(answer.reason, expected)
                self.no_id_files()

    def test_unclaimed_deadline_withdraws_entry_before_check_and_cleans_files(self) -> None:
        self.heartbeat()
        answer = self.attempt()
        self.assertIsInstance(answer, postmaster.RelayRequired)
        assert isinstance(answer, postmaster.RelayRequired)
        self.assertEqual(answer.reason, "unclaimed")
        self.assertAlmostEqual(answer.seconds, postmaster.WAIT_SECONDS, places=6)
        self.no_id_files()

    def test_claim_grace_follows_late_delivery_withdrawal_or_missing_result(self) -> None:
        for outcome in ("delivered", "withdrawn", "missing"):
            with self.subTest(outcome=outcome):
                self.heartbeat()
                start = self.clock.elapsed
                def mod() -> None:
                    self.claim()
                    if self.clock.elapsed - start > postmaster.WAIT_SECONDS + 0.1 and outcome != "missing":
                        self.result("delivered" if outcome == "delivered" else "withdrawn")
                self.clock.after_sleep = mod
                answer = self.attempt()
                if outcome == "delivered":
                    self.assertIsInstance(answer, postmaster.Delivered)
                else:
                    assert isinstance(answer, postmaster.RelayRequired)
                    self.assertEqual(answer.reason, "unclaimed" if outcome == "withdrawn" else "claimed-no-result")
                self.no_id_files()

    def test_half_written_result_is_polled_again(self) -> None:
        self.heartbeat()
        ticks = 0
        def mod() -> None:
            nonlocal ticks
            ticks += 1
            self.claim()
            if ticks == 1:
                self.path("results").parent.mkdir(parents=True, exist_ok=True)
                _ = self.path("results").write_text("{")
            else:
                self.result("delivered")
        self.clock.after_sleep = mod
        self.assertIsInstance(self.attempt(), postmaster.Delivered)
        self.assertEqual(ticks, 2)
        self.no_id_files()

    def test_outbox_write_error_removes_partial_files(self) -> None:
        self.heartbeat()
        with patch.object(os, "replace", side_effect=OSError("cannot publish entry")):
            answer = self.attempt()
        assert isinstance(answer, postmaster.RelayRequired)
        self.assertEqual(answer.reason, "outbox-error")
        self.no_id_files()

    def test_reply_destination_uses_only_one_current_name(self) -> None:
        self.assertEqual(postmaster.reply_destination("Sender", self.live), postmaster.ReplyToSession("sender"))
        self.assertEqual(postmaster.reply_destination("script", self.live), postmaster.ReplyUnroutable())
        self.assertEqual(postmaster.reply_destination("Sender", [self.sender, session("other", "Sender")]), postmaster.ReplyUnroutable())
        former = session("renamed", "New name")
        former["formerNames"] = ["Sender"]
        self.assertEqual(postmaster.reply_destination("Sender", [former]), postmaster.ReplyUnroutable())

    def test_written_wire_entry_has_exact_fields_and_recipient_identity(self) -> None:
        for sender, destination in (("Sender", "sender"), ("script", "")):
            with self.subTest(sender=sender):
                self.heartbeat()
                observed: list[postmaster.OutboxEntry] = []
                deadline = int(self.clock.time() * 1000) + int(postmaster.WAIT_SECONDS * 1000)
                def mod() -> None:
                    value = cast(dict[str, object], json.loads(self.path("outbox").read_text()))
                    self.assertEqual(set(value), {"id", "session_id", "socket", "text", "sha256", "deadline_ms", "sender", "reply_to_session_id"})
                    self.assertRegex(str(value["id"]), r"^[a-fA-F0-9]{32}$")
                    self.assertRegex(str(value["sha256"]), r"^[a-f0-9]{64}$")
                    self.assertIs(type(value["deadline_ms"]), int)
                    self.assertGreaterEqual(cast(int, value["deadline_ms"]), 0)
                    self.assertLessEqual(cast(int, value["deadline_ms"]), 9007199254740991)
                    self.assertEqual(value["deadline_ms"], deadline)
                    self.assertEqual((value["session_id"], value["socket"], value["sender"], value["reply_to_session_id"], value["text"]),
                        ("recipient", "uds:/test-sockets/recipient.sock", sender, destination, TEXT))
                    assert postmaster.is_outbox_entry(value)
                    observed.append(value)
                    self.result("delivered")
                self.clock.after_sleep = mod
                self.assertIsInstance(self.attempt(sender), postmaster.Delivered)
                self.assertEqual(len(observed), 1)
                self.no_id_files()

    def test_cleanup_keeps_twenty_minute_routes_and_removes_old_files_on_next_entry(self) -> None:
        self.heartbeat()
        old: list[Path] = []
        for folder in ("outbox", "claimed", "results", "delivered"):
            path = self.directory / folder / "late.json"
            self.write(path, {})
            age = 1801 if folder == "delivered" else 601
            os.utime(path, (self.clock.time() - age, self.clock.time() - age))
            old.append(path)
        young = self.directory / "delivered" / "recent.json"
        self.write(young, {})
        os.utime(young, (self.clock.time() - 1200, self.clock.time() - 1200))
        self.respond_on_poll("delivered")
        self.assertIsInstance(self.attempt(), postmaster.Delivered)
        self.assertFalse(any(path.exists() for path in old))
        self.assertTrue(young.exists())

    def test_failed_entry_removal_preserves_claim_result_and_blocks_next_mod_poll(self) -> None:
        self.heartbeat()
        self.respond_on_poll("delivered")
        original_unlink = Path.unlink
        original_replace = os.replace
        def unlink(path: Path, missing_ok: bool = False) -> None:
            if path.parent.name == "outbox":
                raise OSError("entry removal failed")
            original_unlink(path, missing_ok=missing_ok)
        def replace(source: str | Path, target: str | Path) -> None:
            if str(target).endswith(".withdrawn"):
                raise OSError("entry withdrawal failed")
            original_replace(source, target)
        with patch.object(Path, "unlink", unlink), patch.object(os, "replace", replace):
            self.assertIsInstance(self.attempt(), postmaster.Delivered)
        self.assertTrue(self.path("claimed").exists())
        self.assertTrue(self.path("results").exists())
        sent: list[str] = []
        if self.path("outbox").exists() and not self.path("claimed").exists() and not self.path("results").exists():
            sent.append(TEXT)
        self.assertEqual(sent, [])

    def test_all_six_claim_check_and_withdraw_check_orders_deliver_exactly_once(self) -> None:
        orders = [
            ("t1", "t2", "s1", "s2"), ("t1", "s1", "t2", "s2"),
            ("t1", "s1", "s2", "t2"), ("s1", "t1", "t2", "s2"),
            ("s1", "t1", "s2", "t2"), ("s1", "s2", "t1", "t2"),
        ]
        for order in orders:
            with self.subTest(order=order):
                self.heartbeat()
                observed: list[str] = []
                delivered: list[str] = []
                cursor = 0
                original_replace, original_rename, original_exists = os.replace, os.rename, Path.exists
                def mod_steps() -> None:
                    nonlocal cursor
                    while cursor < len(order) and order[cursor].startswith("t"):
                        step = order[cursor]
                        cursor += 1
                        observed.append(step)
                        if step == "t1":
                            self.claim()
                        elif original_exists(self.path("outbox")):
                            delivered.append("mod")
                            self.result("delivered")
                        else:
                            self.result("withdrawn")
                def withdraw(source: str | Path, target: str | Path, action: Callable[[str | Path, str | Path], None]) -> None:
                    nonlocal cursor
                    if Path(source) == self.path("outbox"):
                        mod_steps()
                        self.assertEqual(order[cursor], "s1")
                        observed.append("s1")
                        cursor += 1
                        action(source, target)
                        mod_steps()
                    else:
                        action(source, target)
                def replace(source: str | Path, target: str | Path) -> None:
                    withdraw(source, target, original_replace)
                def rename(source: str | Path, target: str | Path) -> None:
                    withdraw(source, target, original_rename)
                def exists(path: Path) -> bool:
                    nonlocal cursor
                    present = original_exists(path)
                    if path == self.path("claimed") and "s1" in observed and "s2" not in observed:
                        self.assertFalse(original_exists(self.path("outbox")))
                        self.assertEqual(order[cursor], "s2")
                        observed.append("s2")
                        cursor += 1
                        mod_steps()
                    return present
                self.clock.after_sleep = lambda: None
                with patch.object(os, "replace", replace), patch.object(os, "rename", rename), patch.object(Path, "exists", exists):
                    answer = self.attempt()
                if isinstance(answer, postmaster.RelayRequired):
                    self.assertEqual(answer.reason, "unclaimed")
                    delivered.append("relay")
                self.assertEqual(observed, list(order))
                self.assertEqual(len(delivered), 1)
                self.no_id_files()

    def test_entry_validator_matches_mod_rules_and_integer_bounds(self) -> None:
        entry = postmaster.wire_entry(ENTRY_ID, self.recipient, "Sender", postmaster.ReplyToSession("sender"), TEXT, 0)
        self.assertTrue(postmaster.is_outbox_entry(entry))
        self.assertTrue(postmaster.is_outbox_entry({**entry, "deadline_ms": 9007199254740991}))
        self.assertTrue(postmaster.is_outbox_entry({**entry, "id": ENTRY_ID.upper()}))
        self.assertTrue(postmaster.is_outbox_entry({**entry, "socket": "", "text": "", "reply_to_session_id": ""}))
        invalid: list[tuple[str, object]] = [
            ("deadline_ms", 9007199254740992), ("deadline_ms", True), ("deadline_ms", 1.0), ("deadline_ms", -1),
            ("id", "a" * 31), ("id", "g" * 32), ("session_id", ""), ("sender", ""),
            ("socket", 17), ("text", 17), ("sha256", "A" * 64), ("sha256", "a" * 63), ("reply_to_session_id", 17)]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                self.assertFalse(postmaster.is_outbox_entry({**entry, field: value}))
        for field in entry:
            missing = dict(entry)
            del missing[field]
            self.assertFalse(postmaster.is_outbox_entry(missing))
        unresolved = postmaster.wire_entry(ENTRY_ID, self.recipient, "script", postmaster.ReplyUnroutable(), TEXT, 0)
        self.assertEqual(unresolved["reply_to_session_id"], "")
        self.assertEqual(entry["reply_to_session_id"], "sender")

    def test_status_counts_errors_replies_and_removes_only_expired_files(self) -> None:
        self.heartbeat()
        for folder, age in (("outbox", 601), ("claimed", 601), ("results", 601), ("delivered", 1801), ("errors", 7 * 86400 + 1)):
            path = self.directory / folder / "old.json"
            self.write(path, {})
            os.utime(path, (self.clock.time() - age, self.clock.time() - age))
        young = self.directory / "delivered" / "twenty-minutes.json"
        self.write(young, {})
        os.utime(young, (self.clock.time() - 1200, self.clock.time() - 1200))
        self.write(self.directory / "replies" / "answer.json", {})
        output = io.StringIO()
        with patch.object(sessions, "live_sessions", return_value=self.live), redirect_stdout(output):
            self.assertEqual(postmaster.status(self.state), 0)
        self.assertIn("postmaster: running, heartbeat 0.0 s old, worker 1800000000000-test-worker; errors: 0; replies: 1", output.getvalue())
        self.assertEqual(list(self.directory.rglob("old.json")), [])
        self.assertTrue(young.exists())
        for index in range(3):
            path = self.directory / "errors" / f"recent-{index}.json"
            self.write(path, {"at_ms": int(self.clock.time() * 1000), "error": "test failure"})
            os.utime(path, (self.clock.time() - 6 * 86400, self.clock.time() - 6 * 86400))
        output = io.StringIO()
        with patch.object(sessions, "live_sessions", return_value=self.live), redirect_stdout(output):
            self.assertEqual(postmaster.status(self.state), 0)
        self.assertIn("errors: 3; replies: 1", output.getvalue())
        (self.directory / "heartbeat.json").unlink()
        with patch.object(sessions, "live_sessions", return_value=self.live), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(postmaster.status(self.state), 1)
        self.assertIn("postmaster: not running; errors: 3; replies: 1", output.getvalue())

    def test_status_reads_user_zone_for_today_counts(self) -> None:
        with patch.object(postmaster, "user_zone", return_value=self.zone) as zone, patch.object(
            sessions, "live_sessions", return_value=self.live
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(postmaster.status(self.state), 1)
        zone.assert_called_once_with()

    def test_status_prints_heartbeat_age_with_one_decimal(self) -> None:
        self.heartbeat()
        self.clock.elapsed = 0.734212
        output = io.StringIO()
        with patch.object(sessions, "live_sessions", return_value=self.live), redirect_stdout(output):
            self.assertEqual(postmaster.status(self.state), 0)
        self.assertEqual(output.getvalue().splitlines()[0],
            "postmaster: running, heartbeat 0.7 s old, worker 1800000000000-test-worker; errors: 0; replies: 0")

    def test_status_today_counts_use_user_zone_and_ignore_other_dates_and_bad_lines(self) -> None:
        self.heartbeat()
        today = datetime.fromtimestamp(self.clock.time(), self.zone).replace(hour=0, minute=0, second=0, microsecond=0)
        rows: list[dict[str, object]] = [
            {"time": today.isoformat(), "via": "postmaster", "postmaster": "delivered"},
            {"time": (today + timedelta(hours=1)).isoformat(), "via": "relay", "postmaster": "failed"},
            {"time": (today + timedelta(hours=2)).isoformat(), "via": "relay", "postmaster": "relay-only-sender"},
            {"time": (today - timedelta(seconds=1)).isoformat(), "via": "relay", "postmaster": "expired"},
            {"time": (today + timedelta(days=1)).isoformat(), "via": "postmaster", "postmaster": "delivered"},
            {"time": today.isoformat(), "outcome": "sent"},
        ]
        self.state.mkdir(parents=True, exist_ok=True)
        _ = (self.state / "log.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + '\n{\n{"time":"invalid"}\n')
        output = io.StringIO()
        with patch.object(sessions, "live_sessions", return_value=self.live), redirect_stdout(output):
            self.assertEqual(postmaster.status(self.state), 0)
        self.assertIn("today: via postmaster=1, relay=2; postmaster delivered=1, failed=1, relay-only-sender=1", output.getvalue())

    def test_result_removal_failure_does_not_change_confirmed_delivery(self) -> None:
        self.heartbeat()
        self.respond_on_poll("delivered")
        original = Path.unlink
        def unlink(path: Path, missing_ok: bool = False) -> None:
            if path == self.path("results"):
                raise OSError("result cleanup failed")
            original(path, missing_ok=missing_ok)
        with patch.object(Path, "unlink", unlink):
            self.assertIsInstance(self.attempt(), postmaster.Delivered)
        self.assertFalse(self.path("outbox").exists())

    def test_next_written_attempt_removes_claim_and_withdrawn_result_left_by_late_mod(self) -> None:
        self.heartbeat()
        first = self.attempt()
        assert isinstance(first, postmaster.RelayRequired)
        self.assertEqual(first.reason, "unclaimed")
        self.claim()
        self.result("withdrawn")
        stamp = self.clock.time()
        for folder in ("claimed", "results"):
            os.utime(self.path(folder), (stamp, stamp))
        self.clock.elapsed += 601
        self.heartbeat()
        new_id = "f" * 32
        def mod() -> None:
            value: postmaster.OutboxResult = {"id": new_id, "outcome": "delivered", "detail": "",
                "sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
                "claimed_ms": int(self.clock.time() * 1000), "finished_ms": int(self.clock.time() * 1000)}
            self.write(self.directory / "results" / f"{new_id}.json", value)
        self.clock.after_sleep = mod
        self.assertIsInstance(postmaster.attempt(self.state, self.recipient, "Sender", new_id, TEXT, self.live), postmaster.Delivered)
        self.no_id_files()
        self.assertFalse(any(new_id in path.name for path in self.directory.rglob("*")))
