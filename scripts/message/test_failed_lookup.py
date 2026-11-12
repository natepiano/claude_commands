"""Test failed recipient notices with private staff and message state."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.message import failed_lookup, repeat_guard, send, send_budget
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id


class FailedLookupTests(unittest.TestCase):
    root: Path = Path()
    notices: list[tuple[str, str]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = self.root / "config"
        config.mkdir()
        staff_file = self.root / "staff.json"
        self.enterContext(mock.patch.dict(os.environ, {
            "STAFF_FILE": str(staff_file),
            "XDG_STATE_HOME": str(self.root / "state"),
            "CONF_DIR": str(config),
            "CLAUDE_CODE_SESSION_ID": "",
            "CODEX_THREAD_ID": "",
            "CLAUDE_PID": "",
        }))
        _ = (config / "send.conf").write_text(
            "failed_lookup_owners=aws-spending-guard=configured-builds\n"
            + "# range: 0.001..\ndelivery_timeout=5\n"
            + "".join(f"# range: 0.001..\n{key}=1\n" for key in (
                "kill_grace", "remote_transport_seconds", "waits_review_seconds", "caller_margin_seconds")))
        _ = (config / "postmaster.conf").write_text(
            "# range: 0.001..\nwait_seconds=1\n# range: 0.001..\nclaim_grace_seconds=1\n")
        _ = (config / "codex_daemon.conf").write_text("# range: 0.001..\ntimeout_seconds=1\n")
        _ = (config / "production.conf").write_text("UNIT_ANOMALIES_TO=fallback-owner\n")
        top = staff.StaffRow(staff.StaffId(fixture_runtime_id("top-id")), "showrunner", "top", staff.StaffRoot(),
                            current_session=staff.StaffId(fixture_runtime_id("top-current")))
        runner = staff.StaffRow(staff.StaffId(fixture_runtime_id("builds-id")), "showrunner", "builds", staff.ReportsTo(top.id),
                               current_session=staff.StaffId(fixture_runtime_id("builds-current")))
        unit = staff.StaffRow(staff.StaffId(fixture_runtime_id("queue-id")), "unit", "queue", staff.ReportsTo(runner.id),
                             current_session=staff.StaffId(fixture_runtime_id("queue-current")), unit_type=staff.UnitDetails("direct"))
        worker = staff.StaffRow(staff.StaffId(fixture_runtime_id("test-id")), "worker", "test", staff.ReportsTo(unit.id),
                               current_session=staff.StaffId(fixture_runtime_id("test-current")))
        agent = staff.StaffRow(staff.StaffId(fixture_runtime_id("agent-id")), "producer", "analyst", staff.StaffRoot(),
                              current_session=staff.StaffId(fixture_runtime_id("agent-current")))
        other = staff.StaffRow(staff.StaffId(fixture_runtime_id("other-id")), "showrunner", "other", staff.StaffRoot(),
                              current_session=staff.StaffId(fixture_runtime_id("other-current")))
        other_unit = staff.StaffRow(staff.StaffId(fixture_runtime_id("other-queue-id")), "unit", "queue",
                                   staff.ReportsTo(other.id), current_session=staff.StaffId(fixture_runtime_id("other-queue-current")), unit_type=staff.UnitDetails("direct"))
        other_worker = staff.StaffRow(staff.StaffId(fixture_runtime_id("other-test-id")), "worker", "other-test",
                                     staff.ReportsTo(other_unit.id), current_session=staff.StaffId(fixture_runtime_id("other-test-current")))
        _ = staff_file.write_text(json.dumps({
            "schema": 2,
            "rows": [row.to_json() for row in (top, runner, unit, worker, agent, other, other_unit, other_worker)],
        }))
        self.notices = []

        def send_notice(owner: str, text: str) -> None:
            self.notices.append((owner, text))

        _ = self.enterContext(mock.patch.object(failed_lookup, "send_notice", new=send_notice))

    def test_notice_uses_the_staff_parent_or_configured_owner(self) -> None:
        caller = subprocess.run(["ps", "-ww", "-p", str(os.getppid()), "-o", "command="],
                                capture_output=True, text=True, check=True).stdout.strip()
        membership = staff.snapshot()
        for sender, owner in (
            ("test", "queue"),
            ("other-test", "other-queue"),
            ("queue", "builds"),
            ("builds", "top"),
            ("analyst", "fallback-owner"),
            ("top", "fallback-owner"),
            ("aws-spending-guard", "configured-builds"),
            ("unknown-sender", "fallback-owner"),
        ):
            with (self.subTest(sender=sender),
                  mock.patch.dict(os.environ, {"CODEX_THREAD_ID": fixture_runtime_id(f"{sender}-current")})):
                failed_name = "missing-recipient"
                before = len(self.notices)
                failed_lookup.report(sender, failed_name)
                self.assertEqual(len(self.notices), before + 1)
                recipient, text = self.notices[-1]
                expected_row = membership.by_id(staff.StaffId(fixture_runtime_id(f"{owner}-id")))
                if expected_row is not None:
                    self.assertEqual(send.resolve_staff_recipient(recipient, membership), expected_row)
                else:
                    self.assertEqual(recipient, owner)
                self.assertIn(sender, text)
                self.assertIn(failed_name, text)
                self.assertIn("Failures since the last notice: 1", text.splitlines())
                self.assertIn(f"Caller: {caller}", text)
                self.assertEqual(text.splitlines()[0], f"Recipient lookup failed for {failed_name}.")

    def test_daily_notice_counts_failures_since_the_previous_notice(self) -> None:
        sender = "test"
        record_path = repeat_guard.message_state() / "failed_lookups.json"
        for first_attempt in ("delivered", "send failed", "owner failed"):
            with self.subTest(first_attempt=first_attempt):
                failed_name = f"dead-recipient-{first_attempt}"
                before = len(self.notices)
                errors = io.StringIO()
                if first_attempt == "delivered":
                    failed_lookup.report(sender, failed_name)
                    self.assertEqual(len(self.notices), before + 1)
                else:
                    boundary = "send_notice" if first_attempt == "send failed" else "notice_owner"
                    with (mock.patch.object(failed_lookup, boundary, side_effect=OSError(first_attempt)),
                          redirect_stderr(errors)):
                        failed_lookup.report(sender, failed_name)
                    self.assertEqual(len(self.notices), before)
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
                    self.assertEqual(record[sender][failed_name]["count"], 1)
                    self.assertEqual(record[sender][failed_name]["last_notice"], 0)
                    self.assertNotIn("notice_started", record[sender][failed_name])

                failed_lookup.report(sender, failed_name)
                self.assertEqual(len(self.notices), before + 1)
                if first_attempt != "delivered":
                    self.assertIn("Failures since the last notice: 2", self.notices[-1][1].splitlines())
                failed_lookup.report(sender, failed_name)
                self.assertEqual(len(self.notices), before + 1)

                record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
                failure = record[sender][failed_name]
                pending = 2 if first_attempt == "delivered" else 1
                self.assertEqual(failure["count"], pending)
                failure["last_notice"] -= 24 * 60 * 60 + 1
                _ = record_path.write_text(json.dumps(record))

                failed_lookup.report(sender, failed_name)
                self.assertEqual(len(self.notices), before + 2)
                owner, text = self.notices[-1]
                self.assertEqual(owner, fixture_runtime_id("queue-id"))
                self.assertIn(sender, text)
                self.assertIn(failed_name, text)
                self.assertIn(f"Failures since the last notice: {pending + 1}", text.splitlines())
                record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
                self.assertEqual(record[sender][failed_name]["count"], 0)
                self.assertNotIn("notice_started", record[sender][failed_name])
                different = f"different-recipient-{first_attempt}"
                failed_lookup.report(sender, different)
                self.assertEqual(len(self.notices), before + 3)
                self.assertIn(different, self.notices[-1][1])
                self.assertIn("Failures since the last notice: 1", self.notices[-1][1].splitlines())

    def test_interrupted_notice_retries_after_the_send_budget(self) -> None:
        sender, failed_name = "test", "interrupted-recipient"
        with (mock.patch.object(failed_lookup, "send_notice", side_effect=KeyboardInterrupt),
              self.assertRaises(KeyboardInterrupt)):
            failed_lookup.report(sender, failed_name)
        record_path = repeat_guard.message_state() / "failed_lookups.json"
        record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
        self.assertEqual(record[sender][failed_name]["count"], 1)
        self.assertEqual(record[sender][failed_name]["last_notice"], 0)

        failed_lookup.report(sender, failed_name)
        self.assertEqual(self.notices, [])
        record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
        self.assertEqual(record[sender][failed_name]["count"], 2)
        budget = send_budget.seconds()
        record[sender][failed_name]["notice_started"] -= budget + 1
        _ = record_path.write_text(json.dumps(record))

        failed_lookup.report(sender, failed_name)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Failures since the last notice: 3", self.notices[-1][1].splitlines())
        record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
        self.assertEqual(record[sender][failed_name]["count"], 0)
        self.assertNotIn("notice_started", record[sender][failed_name])

    def test_failure_during_notice_delivery_remains_for_the_next_notice(self) -> None:
        sender, failed_name = "test", "busy-recipient"
        record_path = repeat_guard.message_state() / "failed_lookups.json"

        def send_with_another_failure(owner: str, text: str) -> None:
            failed_lookup.report(sender, failed_name)
            record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
            self.assertEqual(record[sender][failed_name]["count"], 2)
            self.assertEqual(record[sender][failed_name]["last_notice"], 0)
            self.notices.append((owner, text))

        with mock.patch.object(failed_lookup, "send_notice", new=send_with_another_failure):
            failed_lookup.report(sender, failed_name)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Failures since the last notice: 1", self.notices[-1][1].splitlines())
        record = cast(dict[str, dict[str, dict[str, int | float]]], json.loads(record_path.read_text()))
        self.assertEqual(record[sender][failed_name]["count"], 1)
        record[sender][failed_name]["last_notice"] -= 24 * 60 * 60 + 1
        _ = record_path.write_text(json.dumps(record))
        failed_lookup.report(sender, failed_name)
        self.assertEqual(len(self.notices), 2)
        self.assertIn("Failures since the last notice: 2", self.notices[-1][1].splitlines())

    def test_configuration_paths_require_conf_suffix_or_explicit_option(self) -> None:
        for command, expected in (
            ("job --paths x.json --report y.toml", []),
            ("job config/job.conf --paths x.json", ["config/job.conf"]),
            ("job --config x.json --config-file=y.toml", ["x.json", "y.toml"]),
            ("job --config-file settings --config=options", ["settings", "options"]),
        ):
            with self.subTest(command=command):
                self.assertEqual(failed_lookup.configuration_files(command), expected)

    def test_notice_sender_records_no_failure_and_sends_no_notice(self) -> None:
        failed_lookup.report("test", "dead-recipient")
        record_path = repeat_guard.message_state() / "failed_lookups.json"
        before = record_path.read_bytes()
        failed_lookup.report("send.py", "missing-owner")
        self.assertEqual(record_path.read_bytes(), before)
        self.assertEqual(len(self.notices), 1)
