#!/usr/bin/env python3
"""CLI tests for a unit delegate's notifier gate and instance."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override


REPO = Path(__file__).resolve().parents[2]
PY = REPO / "scripts" / "lib" / "py"
CHECK = REPO / "scripts" / "hooks" / "delegate_run.py"
UNIT_NOTIFIER = REPO / "scripts" / "delegate" / "unit_notifier.sh"


class DelegateCheckTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    active_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    state_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_id: str = "test-claude-session"

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session_dir = self.root / "delegate-run"
        self.session_dir.mkdir()
        self.active_dir = self.root / "active"
        self.active_dir.mkdir()
        self.state_dir = self.root / "notifier"
        self.config_file = self.root / "delegate.conf"

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PLAN_DELEGATE_ACTIVE_DIR"] = str(self.active_dir)
        environment["NOTIFIER_STATE_DIR"] = str(self.state_dir)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["NOTIFIER_NOW_EPOCH"] = "20100"
        return environment

    def write_marker(self, session_dir: Path | None = None) -> None:
        _ = (self.active_dir / self.session_id).write_text(
            str(session_dir or self.session_dir) + "\n", encoding="utf-8"
        )

    def run_check(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PY), str(CHECK), "check", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

    def run_unit_notifier(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["zsh", str(UNIT_NOTIFIER), self.session_id, *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

    def test_check_exits_two_for_missing_or_replaced_marker(self) -> None:
        self.assertEqual(self.run_check(self.session_id, str(self.session_dir)).returncode, 2)
        self.write_marker(self.root / "another-run")
        self.assertEqual(self.run_check(self.session_id, str(self.session_dir)).returncode, 2)

    def test_check_exits_one_when_no_work_is_running(self) -> None:
        self.write_marker()
        self.assertEqual(self.run_check(self.session_id, str(self.session_dir)).returncode, 1)

    def test_check_recognizes_suffixed_implementation_status(self) -> None:
        self.write_marker()
        _ = (self.session_dir / "impl_status_impl").write_text("implementing\n", encoding="utf-8")
        self.assertEqual(self.run_check(self.session_id, str(self.session_dir)).returncode, 0)

    def test_check_recognizes_review_status(self) -> None:
        self.write_marker()
        _ = (self.session_dir / "review_status_ux").write_text("reviewing\n", encoding="utf-8")
        self.assertEqual(self.run_check(self.session_id, str(self.session_dir)).returncode, 0)

    def test_check_recognizes_active_progress_activity(self) -> None:
        self.write_marker()
        _ = (self.session_dir / "progress_history_state.json").write_text(
            json.dumps({"activity": {"status": "active", "label": "checking tests"}}),
            encoding="utf-8",
        )
        self.assertEqual(self.run_check(self.session_id, str(self.session_dir)).returncode, 0)

    def test_check_usage_error_exits_three(self) -> None:
        self.assertEqual(self.run_check(self.session_id).returncode, 3)

    def test_unit_notifier_creates_held_instance_with_rounded_interval(self) -> None:
        self.write_marker()
        _ = self.config_file.write_text(
            "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=901 # a trailing comment\n",
            encoding="utf-8",
        )
        result = self.run_unit_notifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        conf_path = self.state_dir / f"delegate-{self.session_dir.name}" / "conf"
        conf = dict(
            line.split("=", 1) for line in conf_path.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(conf["TARGET"], f"session:{self.session_id}")
        self.assertEqual(conf["EVERY"], "16")
        self.assertEqual(conf["COMMAND"], "/unit:report")
        self.assertEqual(conf["HOLD"], "1")
        self.assertIn("delegate_run.py check", conf["CHECK"])
        self.assertIn(self.session_id, conf["CHECK"])
        self.assertIn(str(self.session_dir), conf["CHECK"])

    def test_unit_notifier_exits_one_without_marker(self) -> None:
        result = self.run_unit_notifier()
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.state_dir / f"delegate-{self.session_dir.name}").exists())

    def test_unit_notifier_off_then_on_changes_instance_state(self) -> None:
        self.write_marker()
        created = self.run_unit_notifier()
        self.assertEqual(created.returncode, 0, created.stderr)
        state_path = self.state_dir / f"delegate-{self.session_dir.name}" / "state"

        stopped = self.run_unit_notifier("off")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(stopped.stdout.splitlines(), [
            f"progress updates off: delegate-{self.session_dir.name}"
        ])
        self.assertIn("ENABLED=0", state_path.read_text(encoding="utf-8").splitlines())

        started = self.run_unit_notifier("on")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(len(started.stdout.splitlines()), 1)
        self.assertTrue(started.stdout.startswith(
            f"progress updates on: delegate-{self.session_dir.name} next_due="
        ), started.stdout)
        self.assertIn("ENABLED=1", state_path.read_text(encoding="utf-8").splitlines())

    def test_unit_notifier_off_without_marker_leaves_state_untouched(self) -> None:
        result = self.run_unit_notifier("off")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no active delegate run marker:", result.stderr)
        self.assertFalse(self.state_dir.exists())

    def test_unit_notifier_on_with_empty_marker_leaves_state_untouched(self) -> None:
        _ = (self.active_dir / self.session_id).write_text("", encoding="utf-8")
        result = self.run_unit_notifier("on")
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty delegate run marker:", result.stderr)
        self.assertFalse(self.state_dir.exists())

    def test_unit_notifier_rejects_bad_mode_and_extra_arguments(self) -> None:
        self.write_marker()
        for arguments in (("pause",), ("on", "extra")):
            with self.subTest(arguments=arguments):
                result = self.run_unit_notifier(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage: unit_notifier.sh", result.stderr)
        self.assertFalse(self.state_dir.exists())

    def test_unit_notifier_on_passes_missing_instance_failure_through(self) -> None:
        self.write_marker()
        result = self.run_unit_notifier("on")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no such instance: delegate-{self.session_dir.name}", result.stderr)
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    _ = unittest.main()
