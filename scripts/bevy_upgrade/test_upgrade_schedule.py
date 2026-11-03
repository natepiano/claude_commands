"""Keep scheduling and delivery isolated behind argument-recording tools."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upgrade_record
import upgrade_schedule

if TYPE_CHECKING:
    from typing import override
else:
    def override(method: object) -> object:
        return method


@final
class ScheduleTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="bevy-schedule-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        notifier = self.root / "notifier.zsh"
        _ = notifier.write_text(
            'printf "%s\\n" "$@" >> "$NOTIFIER_CALLS"\n'
            + 'if [[ "$1" = status ]]; then\n'
            + '  print -r -- "test job"\n'
            + '  print -r -- "next_due=1791850000 (local time)"\n'
            + '  exit "$STATUS_EXIT"\nfi\n', encoding="utf-8")
        sender = self.root / "send.py"
        _ = sender.write_text(
            '#!/usr/bin/env python3\nimport json, os, sys\nfrom pathlib import Path\n'
            + 'Path(os.environ["SEND_CALLS"]).write_text(json.dumps(sys.argv[1:]))\n'
            + 'if sys.argv[1] == "retire":\n'
            + '    print(os.environ["RETIRE_OUTCOME"])\n'
            + '    raise SystemExit(int(os.environ["RETIRE_EXIT"]))\n'
            + 'print(os.environ["SEND_OUTCOME"])\nraise SystemExit(int(os.environ["SEND_EXIT"]))\n',
            encoding="utf-8")
        sender.chmod(0o755)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BEVY_UPGRADE_NOTIFIER": str(notifier), "BEVY_UPGRADE_SEND": str(sender),
            "NOTIFIER_CALLS": str(self.root / "notifier-calls"), "SEND_CALLS": str(self.root / "send-calls"),
            "STATUS_EXIT": "0", "SEND_OUTCOME": "SENT: test recipient", "SEND_EXIT": "0",
            "RETIRE_OUTCOME": "retired 1 queued message", "RETIRE_EXIT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        })))

    def record(self) -> upgrade_record.Record:
        return {"slug": "demo", "production": str(self.root / "doc with space.md"),
                "checkout": str(self.root), "target": "0.20", "started": "2026-10-12T00:00:00+00:00",
                "closed": None, "bevy": [], "pins_read": "2026-10-12T00:00:00+00:00",
                "dependencies": {}, "target_bevy": {"status": "unread"}, "target_error": "",
                "checked": None, "announced": None}

    def test_instance_and_new_command_quote_each_run_word(self) -> None:
        current = self.record()
        self.assertEqual(upgrade_schedule.instance("demo"), "bevy-upgrade-demo")
        upgrade_schedule.ensure_job(current)
        args = (self.root / "notifier-calls").read_text().splitlines()
        scripts = Path(__file__).resolve().parents[1]
        words = [str(scripts / "lib/py"), str(scripts / "bevy_upgrade/bevy_upgrade.py"),
                 "--production", current["production"], "check", "--scheduled"]
        self.assertEqual(args, ["new", "bevy-upgrade-demo", "--every", "240", "--timeout", "300",
                                "--run", " ".join(shlex.quote(word) for word in words)])
        self.assertEqual(shlex.split(args[-1]), words)

    def test_remove_names_only_the_upgrade_job(self) -> None:
        upgrade_schedule.remove_job("demo")
        self.assertEqual((self.root / "notifier-calls").read_text().splitlines(),
                         ["remove", "bevy-upgrade-demo"])

    def test_next_check_reads_epoch_and_missing_job_is_none(self) -> None:
        self.assertEqual(upgrade_schedule.next_check("demo"), datetime.fromtimestamp(1791850000, timezone.utc))
        with mock.patch.dict(os.environ, {"STATUS_EXIT": "1"}):
            self.assertIsNone(upgrade_schedule.next_check("demo"))

    def test_sent_message_has_exact_recipient_key_summary_and_text(self) -> None:
        with mock.patch("upgrade_schedule.showrunners.current_address", return_value="uds:/fake/session"):
            self.assertTrue(upgrade_schedule.tell_showrunner("demo", "Two changes", "line one\nline two"))
        args = cast(list[str], json.loads((self.root / "send-calls").read_text()))
        self.assertEqual(args, ["--to", "uds:/fake/session", "--from", "bevy-upgrade", "--key",
                                "bevy-upgrade-demo", "--summary", "Two changes", "--text", "line one\nline two"])

    def test_empty_address_sends_nothing_and_explains_non_delivery(self) -> None:
        stderr = io.StringIO()
        with mock.patch("upgrade_schedule.showrunners.current_address", return_value=""), contextlib.redirect_stderr(stderr):
            self.assertFalse(upgrade_schedule.tell_showrunner("demo", "changes", "body"))
        self.assertFalse((self.root / "send-calls").exists())
        self.assertEqual(stderr.getvalue(), "bevy_upgrade: the showrunner was not told: no showrunner session\n")

    def test_every_non_sent_outcome_preserves_delivery_failure(self) -> None:
        for code, outcome in ((0, "SKIPPED: acknowledged"), (1, "QUEUED: session away"),
                              (2, "REFUSED: invalid address"), (3, "FAILED: socket closed")):
            with self.subTest(outcome=outcome), mock.patch.dict(os.environ, {
                "SEND_OUTCOME": outcome, "SEND_EXIT": str(code),
            }), mock.patch("upgrade_schedule.showrunners.current_address", return_value="uds:/fake/session"):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertFalse(upgrade_schedule.tell_showrunner("demo", "changes", "body"))
                self.assertEqual(stderr.getvalue(), f"bevy_upgrade: the showrunner was not told: stdout: {outcome}\n")

    def test_retirement_names_recipient_sender_and_key_and_suppresses_success_output(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("upgrade_schedule.showrunners.current_address", return_value="uds:/fake/session"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            upgrade_schedule.retire_message("demo")
        args = cast(list[str], json.loads((self.root / "send-calls").read_text()))
        self.assertEqual(args, ["retire", "--to", "uds:/fake/session", "--from", "bevy-upgrade",
                                "--key", "bevy-upgrade-demo"])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_retirement_without_showrunner_address_runs_nothing(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("upgrade_schedule.showrunners.current_address", return_value=""), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            upgrade_schedule.retire_message("demo")
        self.assertFalse((self.root / "send-calls").exists())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_failed_retirement_reports_sender_output_without_raising(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"RETIRE_EXIT": "3", "RETIRE_OUTCOME": "FAILED: queue inaccessible"}), \
                mock.patch("upgrade_schedule.showrunners.current_address", return_value="uds:/fake/session"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            upgrade_schedule.retire_message("demo")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(),
                         "bevy_upgrade: could not retire the queued message: stdout: FAILED: queue inaccessible\n")

    def test_retirement_os_error_reports_failure_without_raising(self) -> None:
        stderr = io.StringIO()
        with mock.patch("upgrade_schedule.showrunners.current_address", return_value="uds:/fake/session"), \
                mock.patch("upgrade_schedule.subprocess.run", side_effect=OSError("sender unavailable")) as runner, \
                contextlib.redirect_stderr(stderr):
            upgrade_schedule.retire_message("demo")
        scripts = Path(__file__).resolve().parents[1]
        runner.assert_called_once_with(
            [str(scripts / "lib/py"), str(self.root / "send.py"), "retire", "--to", "uds:/fake/session",
             "--from", "bevy-upgrade", "--key", "bevy-upgrade-demo"],
            capture_output=True, text=True, check=False)
        self.assertEqual(stderr.getvalue(),
                         "bevy_upgrade: could not retire the queued message: sender unavailable\n")


if __name__ == "__main__":
    _ = unittest.main()
