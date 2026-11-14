"""Check hold dispatch and hold events for configured SSH targets."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import subprocess
from typing import override
from unittest import mock
import unittest

from scripts.mac_test.test_target_state import IsolatedTargetState, SCRIPTS, TARGET_NAMES, build_hold
from scripts.production.staff_test_support import fixture_member, isolated_staff_file

ORIGINAL_RUN = subprocess.run
ORIGINAL_POPEN = subprocess.Popen


class TargetHoldTests(IsolatedTargetState):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.config.write_text(self.config.read_text() + "sync.hosts.aws=aws-build\n")
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

    def test_unsupported_targets_do_not_change_any_target(self) -> None:
        before = self.snapshot()
        for action in ("clear", "drain", "release"):
            for name in (*TARGET_NAMES, "aws", "all"):
                with self.subTest(action=action, target=name):
                    arguments = [action, name]
                    if action != "release":
                        arguments.extend(["--for", "private test"])
                    result = self.invoke_hold(arguments)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, f"{action} {name} is not supported yet\n")
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(self.snapshot(), before)

    def test_default_and_explicit_natedev_clear_drain_and_release_use_local_records(self) -> None:
        _ = self.config.write_text(self.config.read_text() + "default_target=natedev\n")
        (self.root / "proc").mkdir(exist_ok=True)
        for action in ("clear", "drain"):
            for selection in ([], ["natedev"]):
                with self.subTest(action=action, selection=selection), \
                     mock.patch.object(build_hold.hold_enforcer(), "start"), \
                     mock.patch.object(build_hold.hold_enforcer(), "wait_for_kept_runs", return_value=0), \
                     mock.patch.object(build_hold, "print_hold_audit"), \
                     mock.patch.object(build_hold, "record_hold_event"):
                    result = self.invoke_hold([action, *selection, "--holder", "alice", "--for", "inspect local",
                                               "--proc-root", str(self.root / "proc")])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    path = self.root / "holders" / build_hold.holder_of("alice").key
                    self.assertTrue(path.is_file())
                    result = self.invoke_hold(["release", *selection, "--holder", "alice"])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertFalse(path.exists())

    def test_remote_helpers_remain_available(self) -> None:
        result = build_hold.run_mac_hold(["block", "--holder", "alice", "--for", "private test"])
        self.assertEqual(result.status, 0, result.stdout + result.stderr)
        result = build_hold.release_mac_hold(build_hold.HolderIdentity("alice", "alice"))
        self.assertEqual(result.status, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
