"""Check hold dispatch and hold events for configured SSH targets."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import subprocess
from typing import override
from unittest import mock
import unittest

from scripts.mac_test.test_target_state import IsolatedTargetState, SCRIPTS, TARGET_NAMES, build_hold, mac_test, targets
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production import staff

ORIGINAL_RUN = subprocess.run
ORIGINAL_POPEN = subprocess.Popen


class TargetHoldTests(IsolatedTargetState):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        for name in ("alice", "bob"):
            _ = fixture_member("producer", name=name)
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=self.target_command))

    def target_command(self, arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ["git", "-C"]:
            with mock.patch.object(subprocess, "Popen", ORIGINAL_POPEN):
                return ORIGINAL_RUN(arguments, capture_output=True, text=True, check=False)
        self.assertEqual(arguments[:2], [str(SCRIPTS / "lib/py"), str(SCRIPTS / "mac_test/mac_test.py")])
        self.assertTrue(options.get("capture_output"))
        self.assertTrue(options.get("text"))
        return self.invoke_mac(arguments[2:])

    def invoke_hold(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                status = build_hold.main(arguments)
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        return subprocess.CompletedProcess(arguments, status, output.getvalue(), errors.getvalue())

    def test_mac_hold_uses_registry_settings_for_both_target_forms(self) -> None:
        settings = self.root / "registered-mac.conf"
        _ = settings.write_text((self.root / "mac.conf").read_text().replace("block_hours=4", "block_hours=1"),
                                encoding="utf-8")
        _ = self.config.write_text(self.config.read_text().replace("settings.mac=mac.conf",
                                                                  "settings.mac=registered-mac.conf"),
                                   encoding="utf-8")
        for selection in (["--target", "mac"], ["target", "mac"]):
            with self.subTest(selection=selection):
                result = self.invoke_hold(["hold", "--holder", "alice", "--for", "inspect mac", *selection])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                block = mac_test.read_claim_block(mac_test.state_paths("mac"))
                self.assertNotIsInstance(block, mac_test.NoMacBlock)
                if not isinstance(block, mac_test.NoMacBlock):
                    self.assertEqual(block.expires - block.since, timedelta(hours=1))
                self.assertTrue((self.mac_state / "block.json").is_file())
                result = self.invoke_hold(["release", "--holder", "alice", *selection])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse((self.mac_state / "block.json").exists())
        member = fixture_member("producer", name="before-rename")
        result = self.invoke_hold(["hold", "--target", "mac", "--holder", member.name, "--for", "rename hold"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        block = mac_test.read_claim_block(mac_test.state_paths("mac"))
        assert not isinstance(block, mac_test.NoMacBlock)
        self.assertEqual(block.holder, member.id)
        member = staff.rename(member.id, "after-rename")
        status = self.invoke_mac(["status", "--target", "mac"])
        self.assertIn("blocked by after-rename since ", status.stdout)
        refused = self.invoke_hold(["release", "--target", "mac", "--holder", "before-rename"])
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        released = self.invoke_hold(["release", "--target", "mac", "--holder", member.name])
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        self.assertFalse((self.mac_state / "block.json").exists())

    def test_unconfigured_mac_hold_refuses_before_state_writes(self) -> None:
        _ = self.config.write_text(self.config.read_text().replace("targets=mac,", "targets="), encoding="utf-8")
        before = self.snapshot()
        for selection in (["--target", "mac"], ["target", "mac"]):
            with self.subTest(selection=selection):
                result = self.invoke_hold(["hold", "--holder", "alice", "--for", "inspect mac", *selection])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("mac", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_unconfigured_mac_release_preserves_existing_hold(self) -> None:
        result = self.block("mac")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _ = self.config.write_text(self.config.read_text().replace("targets=mac,", "targets="), encoding="utf-8")
        before = self.snapshot()
        for selection in (["--target", "mac"], ["target", "mac"]):
            with self.subTest(selection=selection):
                result = self.invoke_hold(["release", "--holder", "alice", *selection])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("mac", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_default_mac_helpers_refuse_unconfigured_target_before_state_writes(self) -> None:
        _ = self.config.write_text(self.config.read_text().replace("targets=mac,", "targets="), encoding="utf-8")
        before = self.snapshot()
        result = build_hold.run_mac_hold(["block", "--holder", "alice", "--for", "inspect mac"])
        self.assertEqual(result.status, 2, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        result = build_hold.release_mac_hold(build_hold.HolderIdentity("alice", "alice"))
        self.assertEqual(result.status, 2, result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_holds_and_release_use_independent_target_records(self) -> None:
        for name in TARGET_NAMES:
            result = self.invoke_hold(["hold", "--target", name, "--holder", "alice", "--for", f"inspect {name}"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.claim(name).returncode, 10)
        before = {name: (self.directory(name) / "block.json").read_bytes() for name in ("mac", "linux-two")}
        result = self.invoke_hold(["release", "--target", "linux", "--holder", "alice"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(targets.target_state(self.selected_target("linux")), targets.Free)
        for name in ("mac", "linux-two"):
            self.assertEqual((self.directory(name) / "block.json").read_bytes(), before[name])
            self.assertEqual(self.claim(name).returncode, 10)
        self.assertFalse((self.root / "holders").exists())
        self.assertFalse((self.root / "release").exists())

    def test_target_word_syntax_selects_the_same_hold_as_target_option(self) -> None:
        result = self.invoke_hold(["hold", "--holder", "alice", "--for", "inspect linux", "target", "linux"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(targets.target_state(self.selected_target("linux")), targets.Held)
        result = self.invoke_hold(["release", "--target", "linux", "--holder", "alice"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsInstance(targets.target_state(self.selected_target("linux")), targets.Free)

    def test_begin_and_end_events_record_the_actual_target(self) -> None:
        for name in TARGET_NAMES:
            for arguments in (["hold", "--for", f"inspect {name}"], ["release"]):
                result = self.invoke_hold([*arguments, "--target", name, "--holder", "alice"])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([(event["target"], event["event"], event["holder"], event["reason"])
                          for event in self.events()],
                         [(name, event, build_hold.holder_of("alice").key, f"inspect {name}")
                          for name in TARGET_NAMES for event in ("began", "ended")])

    def test_wrong_holder_cannot_release_another_targets_hold(self) -> None:
        result = self.invoke_hold(["hold", "--target", "linux", "--holder", "alice", "--for", "inspect"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        before = self.snapshot()
        result = self.invoke_hold(["release", "--target", "linux", "--holder", "bob"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("alice", result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([(event["target"], event["event"]) for event in self.events()], [("linux", "began")])

    def test_unknown_targets_refuse_without_writes_or_dispatch(self) -> None:
        before = self.snapshot()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("Unknown target must not dispatch")):
            for arguments in (["hold", "--for", "inspect"], ["release"]):
                result = self.invoke_hold([*arguments, "--target", "missing", "--holder", "alice"])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("missing", result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_natedev_hold_uses_only_local_records(self) -> None:
        with mock.patch.object(build_hold.hold_enforcer(), "start"), \
                mock.patch.object(build_hold, "print_hold_audit"), \
                mock.patch.object(build_hold, "record_hold_event"), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("Local hold must not dispatch")):
            result = self.invoke_hold(["hold", "--target", "natedev", "--holder", "alice", "--for", "inspect local"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "holders" / build_hold.holder_of("alice").key).is_file())
        for name in TARGET_NAMES:
            self.assertFalse(self.directory(name).exists())
            self.assertIsInstance(targets.target_state(self.selected_target(name)),
                                  targets.Free)


if __name__ == "__main__":
    _ = unittest.main()
