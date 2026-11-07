"""Keep accepted routing alerts across renders and concurrent requests."""
from __future__ import annotations

import json
import fcntl
import time
import os
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.production import change_routing, staff, unrouted_alert
from scripts.production.staff_test_support import fixture_member, production_staff_record


class UnroutedAlertTests(unittest.TestCase):
    root: Path = Path()
    config: Path = Path()
    row: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root), "STAFF_FILE": str(self.root / "staff.json"),
                                               "REVIEW_LINKS_ROOT": str(self.root / "links")}))
        source_config = Path(__file__).resolve().parents[2] / "config/change_review.conf"
        _ = (self.root / "change_review.conf").write_text(source_config.read_text())
        self.config = self.root / "review_links.conf"
        source = Path(__file__).resolve().parents[2] / "config/review_links.conf"
        _ = self.config.write_text(source.read_text().replace("unrouted_alert_recipient=natedev", "unrouted_alert_recipient=alert-owner"))
        self.row = {"#": "C8", "Change": "A broken producer"}

    def notify(self, reason: str = "conflict") -> None:
        unrouted_alert.notify(self.root / "links", "top", self.row, reason, ("one", "two"))

    def isolated_sender(self) -> Callable[[str, str, str], None]:
        """Use the sender implementation with a fake subprocess, independent of the probe."""
        spec = spec_from_file_location("fixture_alert_sender", unrouted_alert.__file__)
        assert spec is not None and spec.loader is not None
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return cast(Callable[[str, str, str], None], module.send_message)

    def test_importing_the_alert_does_not_load_the_sender(self) -> None:
        command = """import sys
sys.path.insert(0, sys.argv[1])
from scripts.production import unrouted_alert
assert 'scripts.message.send' not in sys.modules
assert 'send' not in sys.modules
"""
        result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-I", "-c", command, str(Path(__file__).resolve().parents[2])],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_pending_alert_failure(self) -> None:
        error = StringIO()
        with patch.object(unrouted_alert, "send_message") as send, redirect_stderr(error):
            self.notify()
        send.assert_not_called()
        self.assertEqual(len(error.getvalue().splitlines()), 1)
        self.assertTrue(error.getvalue().startswith("review-links unrouted alert:"))
        for path in (self.root / "links/.review-link-unrouted").glob("*.json"):
            self.assertIsNot(cast(dict[str, object], json.loads(path.read_text())).get("sent"), True)

    def test_a_missing_retry_setting_reports_one_error_without_sending_or_marking_sent(self) -> None:
        _ = self.config.write_text(self.config.read_text().replace("unrouted_alert_retry_seconds=60", ""))
        self.assert_pending_alert_failure()

    def test_an_unusable_recipient_reports_one_error_and_keeps_the_alert_pending(self) -> None:
        _ = self.config.write_text(self.config.read_text().replace("unrouted_alert_recipient=alert-owner", "unrouted_alert_recipient="))
        self.assert_pending_alert_failure()

    def test_an_unwritable_state_folder_reports_one_error_without_sending(self) -> None:
        with patch.object(Path, "mkdir", side_effect=PermissionError("state folder is read only")):
            self.assert_pending_alert_failure()

    def test_a_new_conflict_sends_once_and_its_record_survives_another_render(self) -> None:
        self.row["Change"] = "A routing conflict"
        with patch.object(unrouted_alert, "send_message") as send:
            self.notify()
            self.notify()
        send.assert_called_once()
        recipient, _, message = cast(tuple[str, str, str], send.call_args.args)
        self.assertEqual(recipient, "alert-owner")
        for text in ("List: top", "Row: C8", "Change: A routing conflict", "Reason: conflict", "Candidates: one, two"):
            self.assertIn(text, message)
        path = next((self.root / "links/.review-link-unrouted").glob("*.json"))
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertIs(record["sent"], True)

    def test_two_concurrent_renders_send_one_alert_without_waiting_on_the_lock(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def send(recipient: str, sender: str, message: str) -> None:
            del recipient, sender, message
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release the alert")
        with patch.object(unrouted_alert, "send_message", side_effect=send) as sent, ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.notify)
            try:
                self.assertTrue(entered.wait(5))
                second = executor.submit(self.notify)
                second.result(5)
                self.assertEqual(sent.call_count, 1)
            finally:
                release.set()
            first.result(5)
        self.notify_after_acceptance()

    def notify_after_acceptance(self) -> None:
        with patch.object(unrouted_alert, "send_message") as send:
            self.notify()
        send.assert_not_called()

    def test_a_changed_reason_sends_one_new_alert_for_the_same_row(self) -> None:
        with patch.object(unrouted_alert, "send_message") as send:
            self.notify()
            self.notify("missing parent")
            self.notify("missing parent")
        self.assertEqual(send.call_count, 2)
        self.assertIn("Reason: missing parent", cast(str, send.call_args.args[2]))

    def test_a_failed_alert_stays_pending_and_retries_after_the_configured_pause(self) -> None:
        error = StringIO()
        with patch.object(time, "time", return_value=100), patch.object(unrouted_alert, "send_message", side_effect=RuntimeError("socket closed")) as send, redirect_stderr(error):
            self.notify()
            self.notify()
        send.assert_called_once()
        self.assertEqual(error.getvalue().splitlines(), ["review-links unrouted alert: socket closed"])
        path = next((self.root / "links/.review-link-unrouted").glob("*.json"))
        self.assertIs(cast(dict[str, object], json.loads(path.read_text()))["sent"], False)
        with patch.object(time, "time", return_value=159), patch.object(unrouted_alert, "send_message") as send:
            self.notify()
        send.assert_not_called()
        with patch.object(time, "time", return_value=160), patch.object(unrouted_alert, "send_message") as send:
            self.notify()
        send.assert_called_once()
        self.notify_after_acceptance()

    def test_a_queued_alert_is_marked_after_acceptance_and_never_sent_again(self) -> None:
        with (patch.object(unrouted_alert, "send_message", side_effect=self.isolated_sender()),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "QUEUED: recipient absent", "")) as send):
            self.notify()
            self.notify()
        send.assert_called_once()
        self.notify_after_acceptance()

    def test_delivered_and_kept_alerts_remain_accepted_after_another_render(self) -> None:
        for header in ("SENT", "KEPT"):
            with self.subTest(header=header):
                with (patch.object(unrouted_alert, "send_message", side_effect=self.isolated_sender()),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, header + ": accepted", "")) as send):
                    self.notify(header)
                    self.notify(header)
                send.assert_called_once()

    def test_unaccepted_send_outcomes_keep_alerts_pending(self) -> None:
        for code, header in ((0, "SKIPPED"), (0, "REPLACED"), (3, "FAILED"), (2, "REFUSED"), (4, "HELD"), (5, "PENDING"), (6, "RETRY")):
            with self.subTest(header=header):
                error = StringIO()
                with (patch.object(unrouted_alert, "send_message", side_effect=self.isolated_sender()),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, header + ": unaccepted", "")), redirect_stderr(error)):
                    self.notify(header)
                self.assertEqual(error.getvalue().splitlines(), ["review-links unrouted alert: " + header + ": unaccepted"])
        for path in (self.root / "links/.review-link-unrouted").glob("*.json"):
            self.assertIs(cast(dict[str, object], json.loads(path.read_text()))["sent"], False)

    def test_inconsistent_send_results_keep_alerts_pending(self) -> None:
        for code, header in ((0, "QUEUED"), (1, "SENT"), (1, "KEPT")):
            with self.subTest(header=header):
                error = StringIO()
                with (patch.object(unrouted_alert, "send_message", side_effect=self.isolated_sender()),
                      patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, header + ": inconsistent", "")), redirect_stderr(error)):
                    self.notify(header)
                self.assertEqual(error.getvalue().splitlines(), ["review-links unrouted alert: " + header + ": inconsistent"])
        for path in (self.root / "links/.review-link-unrouted").glob("*.json"):
            self.assertIs(cast(dict[str, object], json.loads(path.read_text()))["sent"], False)

    def test_a_zero_exit_without_a_send_header_keeps_the_alert_pending(self) -> None:
        error = StringIO()
        with (patch.object(unrouted_alert, "send_message", side_effect=self.isolated_sender()),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), redirect_stderr(error)):
            self.notify()
        self.assertEqual(error.getvalue().splitlines(), ["review-links unrouted alert: message delivery failed"])
        path = next((self.root / "links/.review-link-unrouted").glob("*.json"))
        self.assertIs(cast(dict[str, object], json.loads(path.read_text()))["sent"], False)

    def test_a_held_alert_lock_does_not_wait_or_send(self) -> None:
        with patch.object(fcntl, "flock", side_effect=BlockingIOError), patch.object(unrouted_alert, "send_message") as send:
            self.notify()
        send.assert_not_called()
        self.assertEqual(list((self.root / "links/.review-link-unrouted").glob("*.json")), [])

    def test_the_retry_pause_changes_when_the_live_config_changes(self) -> None:
        with patch.object(time, "time", return_value=100), patch.object(unrouted_alert, "send_message", side_effect=RuntimeError("not reached")), redirect_stderr(StringIO()):
            self.notify()
        with patch.object(time, "time", return_value=110), patch.object(unrouted_alert, "send_message") as send:
            self.notify()
            send.assert_not_called()
            _ = self.config.write_text(self.config.read_text().replace("unrouted_alert_retry_seconds=60", "unrouted_alert_retry_seconds=5"))
            self.notify()
        send.assert_called_once()

    def orphan_router(self) -> tuple[change_routing.StaffRouting, staff.StaffRow, staff.StaffRow]:
        _ = fixture_member("showrunner", name="top")
        runner = staff.snapshot().by_path("top")
        assert runner is not None
        unit = fixture_member("unit", name="orphan", parent=runner.id)
        record = staff.snapshot()
        broken = replace(record, rows=tuple(replace(row, parent=staff.StaffRoot()) if row.id == unit.id else row for row in record.rows))
        return change_routing.StaffRouting(broken, {}), runner, unit

    def test_routed_rows_and_saved_unrouted_rows_read_no_alert_record_and_send_nothing(self) -> None:
        router, runner, unit = self.orphan_router()
        read_text = Path.read_text

        def read_config(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path.suffix != ".conf":
                raise AssertionError("no record read")
            return read_text(path, encoding=encoding, errors=errors)

        with (patch.object(Path, "read_text", new=read_config),
              patch.object(unrouted_alert, "send_message") as send):
            self.assertEqual(router.owner(self.row, self.root / "change-review - top.md").tab, runner.name)
            owner = router.owner(self.row | {"Verdict": "works", "Producer": str(unit.id)}, self.root / "change-review - top.md")
            self.assertIsInstance(owner, change_routing.UnroutedChange)
        send.assert_not_called()
        self.assertFalse((self.root / "links").exists())

    def test_a_failed_alert_keeps_the_broken_producer_unrouted(self) -> None:
        _ = self.enterContext(production_staff_record())
        _ = self.enterContext(patch.object(change_routing, "configured_lists_directory", return_value=self.root))
        router, _, unit = self.orphan_router()
        with (patch.object(unrouted_alert, "send_message", side_effect=RuntimeError("not reached")), redirect_stderr(StringIO())):
            owner = router.owner(self.row | {"Producer": str(unit.id)}, self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        self.assertEqual(owner.tab, "unrouted")

    def test_an_unrouted_list_in_the_real_folder_sends_once_through_the_fake_sender(self) -> None:
        _ = self.enterContext(production_staff_record())
        router, _, unit = self.orphan_router()
        with (patch.object(change_routing, "configured_lists_directory", return_value=self.root),
              patch.object(unrouted_alert, "send_message") as send):
            for _ in range(2):
                owner = router.owner(self.row | {"Producer": str(unit.id)}, self.root / "change-review - top.md")
                self.assertIsInstance(owner, change_routing.UnroutedChange)
        send.assert_called_once()
        self.assertIn("Row: C8", cast(str, send.call_args.args[2]))

    def test_a_fixture_list_leaves_the_alert_storage_untouched(self) -> None:
        router, _, unit = self.orphan_router()
        with patch.object(unrouted_alert, "send_message") as send:
            owner = router.owner(self.row | {"Producer": str(unit.id)}, self.root / "change-review - top.md")
        self.assertIsInstance(owner, change_routing.UnroutedChange)
        send.assert_not_called()
        self.assertFalse((self.root / "links/.review-link-unrouted").exists())
        self.assertFalse((self.root / "links").exists())
