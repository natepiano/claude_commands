"""Read Mac idle settings through the shared configuration reader."""

from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import cast
from unittest import mock

from scripts.lib import conf
from scripts.buildlog import mac_idle_alert
from scripts.buildlog.test_index import use_test_log
from scripts.buildlog.mac_idle_test_support import write_idle_alert, write_idle_config
from scripts.lint.memory_test_support import commit_config_fixture


use_test_log()


class SavedAlertTests(unittest.TestCase):
    def test_alert_without_cause_change_time_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_idle_alert(Path(directory), 1600)
            row = cast(dict[str, object], json.loads(path.read_text()))
            del row["changed"]
            _ = path.write_text(json.dumps(row))
            self.assertEqual(mac_idle_alert.read_alert(path), mac_idle_alert.UnreadableAlert("invalid changed"))


class MacIdleConfigTests(unittest.TestCase):
    def test_conf_dir_values_apply_without_a_second_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_idle_config(Path(directory), "alert_seconds=7\npush_seconds=11\n"
                                     + "stale_seconds=13\nrepeat_minutes=17\nsend_timeout_s=19\n")
            with mock.patch.dict(os.environ, {"CONF_DIR": directory, "MAC_TEST_CONFIG": "/unused"}), \
                 mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
                settings = mac_idle_alert.read_idle_config()
            self.assertEqual(settings, mac_idle_alert.MacIdleConfig(7, 11, 13, 17, 19))
            self.assertEqual([call.args[0] for call in read.call_args_list], [path])

    def test_invalid_values_recover_from_history_without_code_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_idle_config(Path(directory), "alert_seconds=23\n")
            commit_config_fixture(path)
            _ = write_idle_config(Path(directory), "alert_seconds=nan\n")
            errors = io.StringIO()
            with mock.patch.dict(os.environ, {"CONF_DIR": directory}), redirect_stderr(errors):
                settings = mac_idle_alert.read_idle_config()
            self.assertEqual(settings.alert_seconds, 23)
            self.assertIn("alert_seconds='nan'", errors.getvalue())
            path.unlink()
            with mock.patch.dict(os.environ, {"CONF_DIR": directory}), redirect_stderr(io.StringIO()):
                self.assertEqual(mac_idle_alert.read_idle_config().alert_seconds, 23)

    def test_missing_or_invalid_values_without_history_raise_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"CONF_DIR": directory}):
            path = Path(directory) / "mac_test.conf"
            with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                _ = mac_idle_alert.read_idle_config()
            for key in ("alert_seconds", "push_seconds", "stale_seconds", "repeat_minutes", "send_timeout_s"):
                for value in ("0", "-1", "nan", "inf", "bad"):
                    with self.subTest(key=key, value=value):
                        _ = write_idle_config(Path(directory), f"{key}={value}\n")
                        with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                            _ = mac_idle_alert.read_idle_config()
            _ = path.write_text("")
            with redirect_stderr(io.StringIO()), self.assertRaises(conf.Unusable):
                _ = mac_idle_alert.read_idle_config()
