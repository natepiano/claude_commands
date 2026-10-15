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
SCHEDULED = "PLAN_DELEGATE_PROGRESS_UPDATES=scheduled\n"
NOTIFIER = REPO / "scripts" / "message" / "notifier.sh"


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
        # A run started before the rename, under the former folder name, keeps its delegate- instance.
        self.session_dir = self.root / "delegate" / "delegate-run"
        self.session_dir.mkdir(parents=True)
        self.active_dir = self.root / "active"
        self.active_dir.mkdir()
        self.state_dir = self.root / "notifier"
        self.config_file = self.root / "delegate.conf"
        # A schedule is asked for here; the shipped default, on demand, has its own tests.
        _ = self.config_file.write_text(SCHEDULED, encoding="utf-8")

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
            "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=901 # a trailing comment\n" + SCHEDULED,
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

    def test_unit_notifier_names_a_run_under_the_new_folder_name_unit(self) -> None:
        new_run = self.root / "unit" / "new-run"
        new_run.mkdir(parents=True)
        self.write_marker(new_run)
        result = self.run_unit_notifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.state_dir / "unit-new-run" / "conf").is_file())
        self.assertFalse((self.state_dir / "delegate-new-run").exists())

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
        self.assertIn("no active unit run marker:", result.stderr)
        self.assertFalse(self.state_dir.exists())

    def test_unit_notifier_on_with_empty_marker_leaves_state_untouched(self) -> None:
        _ = (self.active_dir / self.session_id).write_text("", encoding="utf-8")
        result = self.run_unit_notifier("on")
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty unit run marker:", result.stderr)
        self.assertFalse(self.state_dir.exists())

    def test_unit_notifier_rejects_bad_mode_and_extra_arguments(self) -> None:
        self.write_marker()
        for arguments in (("pause",), ("on", "extra")):
            with self.subTest(arguments=arguments):
                result = self.run_unit_notifier(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage: unit_notifier.sh", result.stderr)
        self.assertFalse(self.state_dir.exists())

    def test_unit_notifier_on_makes_the_instance_a_run_never_had(self) -> None:
        self.write_marker()
        result = self.run_unit_notifier("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith(
            f"progress updates on: delegate-{self.session_dir.name} next_due="
        ), result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        conf = (self.state_dir / f"delegate-{self.session_dir.name}" / "conf").read_text(encoding="utf-8")
        self.assertIn("COMMAND=/unit:report", conf.splitlines())

    def on_demand(self) -> None:
        _ = self.config_file.write_text(
            "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=900\nPLAN_DELEGATE_PROGRESS_UPDATES=on-demand # asked for\n",
            encoding="utf-8",
        )

    def test_unit_notifier_on_demand_makes_no_schedule(self) -> None:
        self.write_marker()
        self.on_demand()
        result = self.run_unit_notifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"progress updates on demand: delegate-{self.session_dir.name} has no schedule; "
            + "/unit:report on starts one"
        ])
        self.assertFalse((self.state_dir / f"delegate-{self.session_dir.name}").exists())

    def test_unit_notifier_with_no_updates_line_makes_no_schedule(self) -> None:
        self.write_marker()
        _ = self.config_file.write_text("PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=900\n", encoding="utf-8")
        result = self.run_unit_notifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("progress updates on demand:", result.stdout)
        self.assertFalse((self.state_dir / f"delegate-{self.session_dir.name}").exists())

    def test_unit_notifier_on_demand_keeps_the_schedule_a_resumed_run_holds(self) -> None:
        self.write_marker()
        created = self.run_unit_notifier()
        self.assertEqual(created.returncode, 0, created.stderr)
        instance = self.state_dir / f"delegate-{self.session_dir.name}"
        before = (instance / "state").read_text(encoding="utf-8")
        self.on_demand()
        resumed = self.run_unit_notifier()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        # Retargeted, with its clock and its on/off state as they were.
        self.assertEqual((instance / "state").read_text(encoding="utf-8"), before)
        self.assertIn("ENABLED=1", before.splitlines())
        self.assertIn(f"TARGET=session:{self.session_id}", (instance / "conf").read_text(encoding="utf-8").splitlines())

    def test_unit_notifier_on_demand_keeps_a_stopped_schedule_stopped(self) -> None:
        self.write_marker()
        self.assertEqual(self.run_unit_notifier().returncode, 0)
        self.assertEqual(self.run_unit_notifier("off").returncode, 0)
        self.on_demand()
        resumed = self.run_unit_notifier()
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        state = self.state_dir / f"delegate-{self.session_dir.name}" / "state"
        self.assertIn("ENABLED=0", state.read_text(encoding="utf-8").splitlines())
        started = self.run_unit_notifier("on")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn("ENABLED=1", state.read_text(encoding="utf-8").splitlines())

    def run_unit_notifier_as(self, session_id: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["zsh", str(UNIT_NOTIFIER), session_id, *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

    def start_run(self, session_id: str) -> str:
        session_dir = self.root / f"run-{session_id}"
        session_dir.mkdir()
        _ = (self.active_dir / session_id).write_text(f"{session_dir}\n", encoding="utf-8")
        created = self.run_unit_notifier_as(session_id)
        self.assertEqual(created.returncode, 0, created.stderr)
        # A run outside the former folder name gets the new unit- instance.
        return f"unit-{session_dir.name}"

    def instance_files(self, instance: str) -> tuple[list[str], list[str]]:
        directory = self.state_dir / instance
        return (
            (directory / "conf").read_text(encoding="utf-8").splitlines(),
            (directory / "state").read_text(encoding="utf-8").splitlines(),
        )

    def test_unit_notifier_all_retimes_every_run_and_keeps_a_stopped_one_stopped(self) -> None:
        _ = self.config_file.write_text("PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=900\n" + SCHEDULED, encoding="utf-8")
        running = self.start_run("running")
        stopped = self.start_run("stopped")
        self.assertEqual(self.run_unit_notifier_as("stopped", "off").returncode, 0)
        other = subprocess.run(
            ["zsh", str(NOTIFIER), "new", "showrunner-film", "--to", "session:film",
             "--every", "15", "--command", "/showrunner:dailies"],
            check=False, capture_output=True, text=True, env=self.environment(),
        )
        self.assertEqual(other.returncode, 0, other.stderr)
        _ = self.config_file.write_text("PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=3600\n", encoding="utf-8")

        result = self.run_unit_notifier_as("--all")

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        self.assertTrue(lines[0].startswith(f"{running} every 60 min, on, next_due="), lines[0])
        self.assertEqual(lines[1], f"{stopped} every 60 min, off")
        for instance, enabled in ((running, "ENABLED=1"), (stopped, "ENABLED=0")):
            conf, state = self.instance_files(instance)
            self.assertIn("EVERY=60", conf)
            self.assertIn(enabled, state)
        conf, _ = self.instance_files("showrunner-film")
        self.assertIn("EVERY=15", conf)

    def test_unit_notifier_all_takes_minutes_over_the_configured_interval(self) -> None:
        running = self.start_run("running")
        result = self.run_unit_notifier_as("--all", "30")
        self.assertEqual(result.returncode, 0, result.stderr)
        conf, _ = self.instance_files(running)
        self.assertIn("EVERY=30", conf)

    def test_unit_notifier_all_says_so_when_no_run_has_a_schedule(self) -> None:
        result = self.run_unit_notifier_as("--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["no unit run has a report schedule"])

    def test_unit_notifier_all_rejects_a_bad_interval(self) -> None:
        for arguments in (("0",), ("hourly",), ("60", "extra")):
            with self.subTest(arguments=arguments):
                result = self.run_unit_notifier_as("--all", *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage: unit_notifier.sh", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
