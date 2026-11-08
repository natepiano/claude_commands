"""Check live step records with private process and record roots."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import socket
import subprocess
import sys
import time
from typing import cast
import unittest
from unittest.mock import patch

from scripts.direct import step_record as steps
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class StepRecordTests(unittest.TestCase):
    def test_write_reports_the_clock_offset_and_both_record_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {**os.environ, "VERIFY_STEPS_DIR": directory}
            arguments = [sys.executable, str(Path(steps.__file__)), "write", "--call-id", "clock",
                         "--pid", str(os.getpid()), "--repo", directory, "--kind", "test",
                         "--runs-on", "any_target", "--eligible-targets", "mac,aws", "--mac-targets", "mac",
                         "--pinned-target", "mac", "--package", "sample", "--state", "waiting_hold"]
            before = (time.time_ns() - time.monotonic_ns()) // 1000
            result = subprocess.run([*arguments, "--report"], env=environment,
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            after = (time.time_ns() - time.monotonic_ns()) // 1000
            self.assertEqual(result.returncode, 0, result.stderr)
            offset_text, record_text, state_text = result.stdout.splitlines()
            offset = int(offset_text)
            self.assertEqual(Path(record_text), root / "clock.json")
            self.assertEqual(Path(state_text), root / "clock.state")
            # Separate clock reads can cross a microsecond boundary.
            self.assertLessEqual(min(before, after) - 1, offset)
            self.assertLessEqual(offset, max(before, after) + 1)
            with patch.dict(os.environ, environment):
                record = steps.read("clock")
            self.assertIsInstance(record, steps.StepRecord)
            assert isinstance(record, steps.StepRecord)
            self.assertEqual(record.runs_on, steps.AnyTarget(("mac", "aws"), ("mac",)))
            self.assertEqual(record.selection, steps.Pinned("mac"))
            self.assertEqual((record.kind, record.package), ("test", steps.NamedPackage("sample")))
            self.assertTrue((root / "clock.json").is_file())

    def join_record(self, root: Path) -> steps.StepRecord:
        pid = os.getpid()
        record = steps.StepRecord("joined", pid, steps.process_start(pid), root, "test", steps.NamedPackage("sample"),
                                  steps.AnyTarget(("mac", "aws"), ("mac",)),
                                  steps.StepState.WAITING_HOLD, "2026-01-01T00:00:00+00:00",
                                  selection=steps.Pinned("mac"))
        steps.write(record)
        return record

    def test_state_file_words_select_the_location_without_changing_the_record(self) -> None:
        for word, selected in (("waiting_mac", ""), ("on_mac", "mac"), ("on_natedev", "natedev"),
                               ("on_ssh aws", "aws")):
            with self.subTest(word=word), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder}):
                    original = self.join_record(root)
                    path = steps.record_path(original.call_id)
                    before = path.read_bytes()
                    _ = path.with_suffix(".state").write_text(word + "\n")
                    joined = steps.read(original.call_id)
                    self.assertIsInstance(joined, steps.StepRecord)
                    assert isinstance(joined, steps.StepRecord)
                    self.assertEqual(joined.state.value, word.split()[0])
                    self.assertEqual(joined.selected_target, selected)
                    self.assertEqual(joined.runs_on, original.runs_on)
                    self.assertEqual(joined.selection, original.selection)
                    self.assertEqual(joined.package, original.package)
                    self.assertEqual(path.read_bytes(), before)

    def test_open_state_keeps_the_published_target_when_cleanup_removes_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder}):
            original = self.join_record(Path(folder))
            path = steps.record_path(original.call_id)
            state = steps.state_path(original.call_id)
            _ = state.write_text("on_ssh aws\n")
            loads = json.loads

            def cleanup_before_decode(text: str) -> object:
                path.unlink()
                state.unlink()
                return cast(object, loads(text))

            with patch("scripts.direct.step_record.json.loads", side_effect=cleanup_before_decode):
                joined = steps.read(original.call_id)
            self.assertIsInstance(joined, steps.StepRecord)
            assert isinstance(joined, steps.StepRecord)
            self.assertEqual(joined.state, steps.StepState.ON_SSH)
            self.assertEqual(joined.selected_target, "aws")

    def test_writes_tree_state_overrides_the_location_until_the_next_publication(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder}):
            original = self.join_record(Path(folder))
            state = steps.record_path(original.call_id).with_suffix(".state")
            _ = state.write_text("writes_tree local_noncompiling\n")
            joined = steps.read(original.call_id)
            self.assertIsInstance(joined, steps.StepRecord)
            assert isinstance(joined, steps.StepRecord)
            self.assertEqual(joined.runs_on, steps.NatedevOnly("step writes the tree"))
            self.assertEqual(joined.selection, steps.Pinned("natedev"))
            self.assertEqual(joined.state, steps.StepState.LOCAL_NONCOMPILING)
            self.assertEqual(joined.selected_target, "")
            with self.subTest(state="on_natedev"):
                _ = state.write_text("writes_tree on_natedev\n")
                compiling = steps.read(original.call_id)
                self.assertIsInstance(compiling, steps.StepRecord)
                assert isinstance(compiling, steps.StepRecord)
                self.assertEqual(compiling.state, steps.StepState.ON_NATEDEV)
                self.assertEqual(compiling.selected_target, "natedev")
                self.assertEqual(compiling.runs_on, steps.NatedevOnly("step writes the tree"))
            _ = state.write_text("local_noncompiling\n")
            restored = steps.read(original.call_id)
            self.assertIsInstance(restored, steps.StepRecord)
            assert isinstance(restored, steps.StepRecord)
            self.assertEqual(restored.runs_on, original.runs_on)

    def test_unknown_state_file_word_returns_an_unreadable_record(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder}):
            original = self.join_record(Path(folder))
            _ = steps.record_path(original.call_id).with_suffix(".state").write_text("unknown\n")
            self.assertIsInstance(steps.read(original.call_id), steps.UnreadableRecord)
            self.assertEqual(steps.live_records(), [])

    def test_joined_updated_uses_the_state_file_modification_time(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder}):
            original = self.join_record(Path(folder))
            state = steps.record_path(original.call_id).with_suffix(".state")
            _ = state.write_text("waiting_hold\n")
            timestamp = 1770000000
            os.utime(state, (timestamp, timestamp))
            joined = steps.read(original.call_id)
            self.assertIsInstance(joined, steps.StepRecord)
            assert isinstance(joined, steps.StepRecord)
            self.assertEqual(datetime.fromisoformat(joined.updated), datetime.fromtimestamp(timestamp, timezone.utc))

    def test_start_command_prints_the_process_start_field_as_text(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            process = Path(folder) / "42"
            process.mkdir()
            _ = (process / "stat").write_text("42 (verify (nested) name) S " + "0 " * 18 + "987654321012345 99\n")
            result = subprocess.run([sys.executable, str(Path(steps.__file__)), "start", "42"],
                env={**os.environ, "VERIFY_PROC_ROOT": folder}, capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "987654321012345\n")
            self.assertEqual(result.stderr, "")

    def test_start_command_refuses_missing_ended_or_incomplete_process_records(self) -> None:
        for state in ("missing", "ended", "incomplete"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                process = Path(folder) / "42"
                process.mkdir()
                if state != "missing":
                    content = "42 (verify) Z " + "0 " * 18 + "123 0\n" if state == "ended" else "42 (verify) S 0\n"
                    _ = (process / "stat").write_text(content)
                result = subprocess.run([sys.executable, str(Path(steps.__file__)), "start", "42"],
                    env={**os.environ, "VERIFY_PROC_ROOT": folder}, capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("step refused", result.stderr)

    def test_ssh_record_keeps_target_order_and_selection_through_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "proc/42"
            process.mkdir(parents=True)
            _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "123 0\n")
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder, "VERIFY_PROC_ROOT": str(root / "proc")}):
                record = steps.StepRecord("ssh", 42, "123", root, "check", steps.WorkspacePackages(),
                                          steps.AnyTarget(("aws", "other")), steps.StepState.WAITING_SSH, "now")
                steps.write(record)
                self.assertEqual(steps.read("ssh"), record)
                self.assertFalse(steps.mac_eligible(record))
                steps.write(replace(record, state=steps.StepState.ON_SSH, selected_target="aws"))
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
                steps.write(replace(current, state=steps.StepState.ON_NATEDEV, selected_target="natedev"))
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
                                          steps.AnyTarget(("studio", "aws"), ("studio",)),
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
                          kind="test", package="sample", eligible_targets=["mac", "natedev"], runs_on={"kind": "any_target", "mac_targets": ["mac"]},
                          state="waiting_mac", updated="2026-11-02T10:00:00Z")
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": folder, "VERIFY_PROC_ROOT": str(root / "proc")}):
                _ = (root / "legacy.json").write_text(json.dumps(legacy))
                decoded = steps.read("legacy")
                self.assertIsInstance(decoded, steps.StepRecord)
                if not isinstance(decoded, steps.StepRecord):
                    self.fail("The legacy record must remain readable")
                self.assertIsInstance(decoded.runs_on, steps.AnyTarget)
                steps.write(replace(decoded, state=steps.StepState.ON_MAC, selected_target="mac"))
                current = steps.read("legacy")
                self.assertIsInstance(current, steps.StepRecord)
                if not isinstance(current, steps.StepRecord):
                    self.fail("The updated record must remain readable")
                self.assertEqual(current.state, steps.StepState.ON_MAC)
                self.assertIsInstance(current.runs_on, steps.AnyTarget)
                self.assertEqual(current.call_id, "legacy")
                self.assertEqual(steps.live_records(), [current])

    def test_record_lifecycle_preserves_decision_and_changes_location(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "proc/42"
            process.mkdir(parents=True)
            _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "123 0\n")
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(root / "steps"), "VERIFY_PROC_ROOT": str(root / "proc")}):
                record = steps.StepRecord("c1", 42, "123", root, "check", steps.WorkspacePackages(),
                                          steps.AnyTarget(("mac", "natedev"), ("mac",)), steps.StepState.WAITING_HOLD, "now")
                steps.write(record)
                self.assertEqual(steps.read("c1"), record)
                steps.write(replace(record, state=steps.StepState.ON_MAC, selected_target="mac"))
                current = steps.read("c1")
                self.assertIsInstance(current, steps.StepRecord)
                if not isinstance(current, steps.StepRecord):
                    self.fail("record disappeared")
                self.assertEqual(current.state, steps.StepState.ON_MAC)
                self.assertEqual(current.runs_on, steps.AnyTarget(("mac", "natedev"), ("mac",)))
                self.assertEqual(steps.live_records(), [current])
                self.assertIn('"package": null', (root / "steps/c1.json").read_text())
                steps.record_path("c1").unlink()
                self.assertEqual(steps.read("c1"), steps.MissingRecord("c1"))

    def test_dead_or_reused_process_is_stale_and_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"VERIFY_STEPS_DIR": str(root / "steps"), "VERIFY_PROC_ROOT": str(root / "proc")}):
                record = steps.StepRecord("old", 42, "123", root, "build", steps.WorkspacePackages(),
                                          steps.NatedevOnly("makes natedev artifacts"), steps.StepState.ON_NATEDEV, "now")
                steps.write(record)
                self.assertEqual(steps.read("old"), steps.StaleRecord("old"))
                process = root / "proc/42"
                process.mkdir(parents=True)
                _ = (process / "stat").write_text("42 (verify) S " + "0 " * 18 + "456 0\n")
                self.assertEqual(steps.read("old"), steps.StaleRecord("old"))
                self.assertEqual(steps.live_records(), [])

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
                record = steps.StepRecord("call", 42, "123", Path(folder), "test", steps.NamedPackage("crate"),
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
