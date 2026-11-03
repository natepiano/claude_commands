"""Local settings notices retain delivery evidence across retries."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import cast, override
from unittest.mock import patch

from scripts.settings import local_key_notices as notices


class LocalKeyNoticeTests(unittest.TestCase):
    root: Path = Path()
    ledger: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = self.root / notices.LEDGER
        config = self.root / "config"
        config.mkdir()
        _ = (config / "settings.conf").write_text("local_key_notice_to=fixture-destination\n")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "CONF_DIR": str(config),
                             "XDG_STATE_HOME": str(self.root / "state")})))

    def test_record_saves_pending_keys_without_a_sender_and_skips_identical_writes(self) -> None:
        with patch("subprocess.run", side_effect=AssertionError("record must not send")):
            notices.record(self.root, "fixture", ["firstKey"])
            before = self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns
            with patch.object(notices, "save_history", side_effect=AssertionError("no new keys")):
                notices.record(self.root, "fixture", ["firstKey"])
                notices.record(self.root, "fixture", [])
        self.assertEqual(notices.read_history(self.ledger), {"fixture": {"firstKey": "pending"}})
        self.assertEqual((self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns), before)

    def test_delivery_uses_live_config_the_selector_and_the_shared_timeout(self) -> None:
        notices.record(self.root, "fixture", ["firstKey"])
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture\n")) as send:
            notices.deliver(self.root, "fixture")
        arguments = cast(list[str], send.call_args.args[0])
        self.assertEqual(arguments[:2], [str(self.root / "scripts/lib/py"), str(self.root / "scripts/message/send.py")])
        self.assertEqual(arguments[arguments.index("--to") + 1], "fixture-destination")
        from scripts.message.send import TIMEOUT
        self.assertEqual(send.call_args.kwargs["timeout"], TIMEOUT + 40)
        self.assertEqual(notices.read_history(self.ledger), {"fixture": {"firstKey": "accepted"}})
        _ = (self.root / "config/settings.conf").write_text("local_key_notice_to=changed-destination\n")
        notices.record(self.root, "fixture", ["firstKey", "secondKey"])
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, "QUEUED: fixture\n")) as send:
            notices.deliver(self.root, "fixture")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0][3], "changed-destination")
        with patch("subprocess.run", side_effect=AssertionError("all notices accepted")), \
                patch.object(notices, "save_history", side_effect=AssertionError("no write")):
            notices.deliver(self.root, "fixture")

    def test_timeout_and_failed_outcomes_retain_pending_notices(self) -> None:
        notices.record(self.root, "fixture", ["firstKey"])
        for result in (subprocess.TimeoutExpired("sender", 80), OSError("sender unavailable"),
                       subprocess.CompletedProcess([], 3, "FAILED: fixture\n"),
                       subprocess.CompletedProcess([], 0, "unrecognized output\n")):
            with self.subTest(result=type(result).__name__):
                def attempt(*_arguments: object, **_options: object) -> subprocess.CompletedProcess[str]:
                    if isinstance(result, BaseException):
                        raise result
                    return result

                with patch("subprocess.run", side_effect=attempt), redirect_stderr(io.StringIO()) as errors:
                    notices.deliver(self.root, "fixture")
                self.assertIn("retry pending", errors.getvalue())
                self.assertEqual(notices.read_history(self.ledger)["fixture"]["firstKey"], "pending")

    def test_delivery_identity_prevents_a_second_delivery_after_a_ledger_write_failure(self) -> None:
        notices.record(self.root, "fixture", ["firstKey"])
        delivered: set[str] = set()
        identities: list[str] = []

        def send(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            identity = arguments[arguments.index("--key") + 1]
            identities.append(identity)
            if identity in delivered:
                return subprocess.CompletedProcess(arguments, 0, "SKIPPED: stable identity\n")
            delivered.add(identity)
            return subprocess.CompletedProcess(arguments, 0, "SENT: fixture\n")

        with patch("subprocess.run", side_effect=send):
            with patch.object(notices, "save_history", side_effect=OSError("ledger unavailable")):
                with self.assertRaises(OSError):
                    notices.deliver(self.root, "fixture")
            self.assertEqual(notices.read_history(self.ledger)["fixture"]["firstKey"], "pending")
            notices.deliver(self.root, "fixture")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(identities, [identities[0], identities[0]])
        self.assertEqual(notices.read_history(self.ledger)["fixture"]["firstKey"], "accepted")

    def test_delivery_releases_the_ledger_lock_so_clean_can_record_another_key(self) -> None:
        notices.record(self.root, "fixture", ["firstKey"])

        def send(_arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            with self.ledger.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            notices.record(self.root, "fixture", ["secondKey"])
            return subprocess.CompletedProcess([], 0, "SENT: fixture\n")

        with patch("subprocess.run", side_effect=send):
            notices.deliver(self.root, "fixture")
        self.assertEqual(notices.read_history(self.ledger),
                         {"fixture": {"firstKey": "accepted", "secondKey": "pending"}})

    def test_invalid_history_and_operations_report_errors_without_replacing_history(self) -> None:
        _ = self.ledger.write_text('{"fixture":{"firstKey":"invalid"}}\n')
        before = self.ledger.read_bytes()
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(notices.main(["record", str(self.root), "fixture", '["secondKey"]']), 1)
            self.assertEqual(notices.main(["invalid", str(self.root), "fixture", "[]"]), 1)
        self.assertIn("invalid notice status", errors.getvalue())
        self.assertEqual(self.ledger.read_bytes(), before)


if __name__ == "__main__":
    _ = unittest.main()
