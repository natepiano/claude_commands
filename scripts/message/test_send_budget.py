"""Caller budgets follow live delivery and cleanup settings."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.message import send_budget


def write_configs(directory: Path) -> None:
    for name in ("send", "postmaster", "codex_daemon"):
        source = Path(__file__).resolve().parents[2] / "config" / f"{name}.conf"
        _ = (directory / source.name).write_text(source.read_text())


class SendBudgetTests(unittest.TestCase):
    def test_default_budget_tracks_delivery_and_cleanup_changes_without_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            config = Path(directory) / "send.conf"
            before = send_budget.seconds()
            _ = config.write_text(config.read_text().replace("delivery_timeout=40", "delivery_timeout=120")
                                  .replace("kill_grace=10", "kill_grace=80"))
            after = send_budget.seconds()
            self.assertEqual(after - before, 220)
            self.assertGreater(after, 120 + 2 * 80 + 30)

    def test_user_budget_preserves_the_larger_delivery_or_review_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            config = Path(directory) / "send.conf"
            _ = config.write_text(config.read_text().replace("delivery_timeout=40", "delivery_timeout=120")
                                  .replace("kill_grace=10", "kill_grace=80"))
            self.assertEqual(send_budget.user_seconds(60), 400)
            self.assertEqual(send_budget.user_seconds(180), 460)

    def test_remote_transport_setting_changes_both_sender_budgets_without_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            config = Path(directory) / "send.conf"
            caller, remote = send_budget.seconds(), send_budget.remote_seconds(40)
            _ = config.write_text(config.read_text().replace("remote_transport_seconds=30", "remote_transport_seconds=90"))
            self.assertEqual(send_budget.seconds() - caller, 60)
            self.assertEqual(send_budget.remote_seconds(40) - remote, 60)
            self.assertGreater(send_budget.seconds(), send_budget.remote_seconds(40))

    def test_review_and_discovery_settings_extend_the_caller_budget_without_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CONF_DIR": directory}):
            write_configs(Path(directory))
            send = Path(directory) / "send.conf"
            daemon = Path(directory) / "codex_daemon.conf"
            before = send_budget.seconds()
            _ = send.write_text(send.read_text().replace("waits_review_seconds=30", "waits_review_seconds=90"))
            _ = daemon.write_text(daemon.read_text().replace("timeout_seconds=5.0", "timeout_seconds=25.0"))
            self.assertEqual(send_budget.seconds() - before, 160)
