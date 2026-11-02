"""Check live step records with private process and record roots."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import socket
from typing import cast
import unittest
from unittest.mock import patch

from scripts.direct import step_record as steps


class StepRecordTests(unittest.TestCase):
    def test_ssh_record_keeps_target_order_and_selection_through_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "proc/42"
            process.mkdir(parents=True)
            _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "123 0\n")
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder, "VERIFY_PROC_ROOT": str(root / "proc")}):
                record = steps.StepRecord("ssh", 42, "123", root, "check", steps.WorkspacePackages(),
                                          steps.SshAllowed(("aws", "other")), steps.StepState.WAITING_SSH, "now")
                steps.write(record)
                self.assertEqual(steps.read("ssh"), record)
                self.assertFalse(steps.mac_eligible(record))
                steps.state("ssh", steps.StepState.ON_SSH, "aws")
                current = steps.read("ssh")
                self.assertIsInstance(current, steps.StepRecord)
                if not isinstance(current, steps.StepRecord):
                    self.fail("The selected SSH record must remain readable")
                self.assertEqual(current.eligible_targets, ("aws", "other"))
                self.assertEqual(current.selected_target, "aws")
                self.assertEqual(current.state, steps.StepState.ON_SSH)
                self.assertIsInstance(current.package, steps.WorkspacePackages)
                row = cast(dict[str, object], json.loads((root / "ssh.json").read_text()))
                self.assertEqual(row["eligible_targets"], ["aws", "other"])
                self.assertEqual(row["selected_target"], "aws")
                steps.state("ssh", steps.StepState.ON_NATEDEV)
                fallback = steps.read("ssh")
                self.assertIsInstance(fallback, steps.StepRecord)
                if isinstance(fallback, steps.StepRecord):
                    self.assertEqual(fallback.selected_target, "natedev")
                    self.assertEqual(fallback.eligible_targets, ("aws", "other"))

    def test_named_package_and_mac_membership_survive_ssh_record_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder}), patch.object(steps, "process_start", return_value="123"):
                record = steps.StepRecord("mixed", 42, "123", root, "test", steps.NamedPackage("sample"),
                                          steps.SshAllowed(("studio", "aws"), ("studio",)),
                                          steps.StepState.WAITING_HOLD, "now")
                steps.write(record)
                decoded = steps.read("mixed")
                self.assertEqual(decoded, record)
                self.assertTrue(steps.mac_eligible(record))
                self.assertEqual(steps.package_name(record.package), "sample")

    def test_legacy_mac_record_keeps_permission_after_state_update(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "proc/42"
            process.mkdir(parents=True)
            _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "123 0\n")
            legacy = dict(v=1, call_id="legacy", pid=42, proc_start="123", repo=folder,
                          kind="test", package="sample", runs_on={"kind": "mac_allowed"},
                          state="waiting_mac", updated="2026-11-02T10:00:00Z")
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder, "VERIFY_PROC_ROOT": str(root / "proc")}):
                _ = (root / "legacy.json").write_text(json.dumps(legacy))
                decoded = steps.read("legacy")
                self.assertIsInstance(decoded, steps.StepRecord)
                if not isinstance(decoded, steps.StepRecord):
                    self.fail("The legacy record must remain readable")
                self.assertIsInstance(decoded.runs_on, steps.MacAllowed)
                steps.state("legacy", steps.StepState.ON_MAC)
                current = steps.read("legacy")
                self.assertIsInstance(current, steps.StepRecord)
                if not isinstance(current, steps.StepRecord):
                    self.fail("The updated record must remain readable")
                self.assertEqual(current.state, steps.StepState.ON_MAC)
                self.assertIsInstance(current.runs_on, steps.MacAllowed)
                self.assertEqual(current.call_id, "legacy")
                self.assertEqual(steps.live_records(), [current])

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
