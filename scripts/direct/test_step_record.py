"""Check live step records with private process and record roots."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import socket
import subprocess
import sys
from typing import cast
import unittest
from unittest.mock import patch

from scripts.direct import step_record as steps


class StepRecordTests(unittest.TestCase):
    def test_cli_updates_records_without_importing_argument_or_dataclass_machinery(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / "proc/42"
            proc.mkdir(parents=True)
            _ = (proc / "stat").write_text("42 (verify) S " + "0 " * 18 + "123 0\n")
            boot = root / "proc/sys/kernel/random"
            boot.mkdir(parents=True)
            _ = (boot / "boot_id").write_text("boot")
            environment = {**os.environ, "VERIFY_STEPS_DIR": folder,
                           "VERIFY_PROC_ROOT": str(root / "proc"), "BUILDLOG_DIR": str(root / "log")}
            launcher = """import runpy, sys

def reject_optional_import(event, arguments):
    if event == "import" and arguments[0] in ("argparse", "dataclasses", "inspect"):
        raise RuntimeError("unnecessary CLI import: " + arguments[0])

sys.addaudithook(reject_optional_import)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
            operations = (
                (["write", "--call-id", "cli", "--pid", "42", "--repo", folder, "--kind", "lint",
                  "--runs-on", "natedev_only", "--reason", "local lint", "--state", "waiting_hold"], 0),
                (["state", "--call-id", "cli", "on_natedev"], 0),
                (["stopped", "--call-id", "cli", "--consumed", str(root / "consumed")], 1),
                (["remove", "--call-id", "cli"], 0),
            )
            for arguments, expected_status in operations:
                with self.subTest(command=arguments[0]):
                    result = subprocess.run([sys.executable, "-c", launcher, str(Path(steps.__file__)),
                                             *arguments], env=environment, capture_output=True, text=True,
                                            timeout=30)
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")
                    if arguments[0] == "write":
                        row = cast(dict[str, object], json.loads((root / "cli.json").read_text()))
                        self.assertEqual(row["runs_on"], {"kind": "natedev_only", "reason": "local lint"})
                        self.assertEqual(row["package"], None)
                    elif arguments[0] == "state":
                        self.assertEqual(cast(dict[str, object], json.loads((root / "cli.json").read_text()))["state"], "on_natedev")
            self.assertFalse((root / "cli.json").exists())

    def test_record_lifecycle_preserves_decision_and_changes_location(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "proc/42"
            process.mkdir(parents=True)
            _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "123 0\n")
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(root / "steps"), "VERIFY_PROC_ROOT": str(root / "proc")}):
                record = steps.StepRecord("c1", 42, "123", root, "check", None,
                                          steps.MacAllowed(), steps.StepState.WAITING_HOLD, "now")
                steps.write(record)
                self.assertEqual(steps.read("c1"), record)
                steps.state("c1", steps.StepState.ON_MAC)
                current = steps.read("c1")
                self.assertIsInstance(current, steps.StepRecord)
                if not isinstance(current, steps.StepRecord):
                    self.fail("record disappeared")
                self.assertEqual(current.state, steps.StepState.ON_MAC)
                self.assertEqual(current.runs_on, steps.MacAllowed())
                self.assertEqual(steps.live_records(), [current])
                self.assertIn('"package": null', (root / "steps/c1.json").read_text())
                steps.remove("c1")
                self.assertEqual(steps.read("c1"), steps.MissingRecord("c1"))

    def test_dead_or_reused_process_is_stale_and_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(root / "steps"), "VERIFY_PROC_ROOT": str(root / "proc")}):
                record = steps.StepRecord("old", 42, "123", root, "build", None,
                                          steps.NatedevOnly("makes natedev artifacts"), steps.StepState.ON_NATEDEV, "now")
                steps.write(record)
                self.assertEqual(steps.read("old"), steps.StaleRecord("old"))
                process = root / "proc/42"
                process.mkdir(parents=True)
                _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "456 0\n")
                self.assertEqual(steps.read("old"), steps.StaleRecord("old"))
                self.assertEqual(steps.live_records(), [])
                with self.assertRaises(ValueError):
                    steps.state("old", steps.StepState.ON_MAC)

    def test_invalid_record_has_no_run_permission(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder, "VERIFY_PROC_ROOT": folder + "/proc"}):
                for content in ('{', '{}', '{"v":1,"call_id":"broken","pid":true}'):
                    _ = (Path(folder) / "broken.json").write_text(content)
                    self.assertIsInstance(steps.read("broken"), steps.UnreadableRecord)
                    self.assertEqual(steps.live_records(), [])
                self.assertIsInstance(steps.read("../outside"), steps.UnreadableRecord)

    def test_atomic_update_leaves_only_complete_record(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder}):
                record = steps.StepRecord("call", 42, "123", Path(folder), "test", "crate",
                                          steps.NatedevOnly("linux_only"), steps.StepState.WAITING_HOLD, "now")
                steps.write(record)
                steps.write(replace(record, state=steps.StepState.ON_NATEDEV))
                self.assertEqual([path.name for path in Path(folder).iterdir()], ["call.json"])
                self.assertIn('"state": "on_natedev"', (Path(folder) / "call.json").read_text())

    def test_stopped_build_is_consumed_once_and_other_calls_or_boots_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc = root / "proc/sys/kernel/random"
            proc.mkdir(parents=True)
            _ = (proc / "boot_id").write_text("boot")
            log = root / "log" / socket.gethostname().split(".")[0]
            log.mkdir(parents=True)
            row = dict(kind="hold_build", action="stopped", call_id="call", boot_id="boot",
                       pid=42, proc_start="123", id="stop-id")
            _ = (log / "hold-builds-test.jsonl").write_text("\n".join(json.dumps(value) for value in (
                {**row, "call_id": "other"}, {**row, "boot_id": "old"}, row)))
            with patch.dict(os.environ, {"BUILDLOG_DIR": str(root / "log"), "VERIFY_PROC_ROOT": str(root / "proc")}):
                consumed = root / "consumed"
                self.assertTrue(steps.consume_stopped_build("call", consumed))
                self.assertFalse(steps.consume_stopped_build("call", consumed))
                self.assertFalse(steps.consume_stopped_build("unrelated", root / "unused"))


if __name__ == "__main__":
    _ = unittest.main()
