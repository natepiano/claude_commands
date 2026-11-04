"""Keep token ownership correct between remote and local lint steps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest import mock

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, RUNNER_STUB
from scripts.direct.verify_step_routes_test_support import TARGETS


class VerifyTokenMacStepTests(VerifyMacOffloadFixture):
    def prepare_memory_lint(self) -> Path:
        Path(self.environment["BUILD_HOLD_DIR"]).mkdir()
        targets = self.root / "targets.py"
        _ = targets.write_text(TARGETS)
        reader = self.root / "memory-lint-config.sh"
        _ = reader.write_text(
            'lint_config_enabled() { [[ "$1" == mend || "$1" == fmt ]]; }\n'
            + 'lint_config_skip_notice() { :; }\n')
        self.environment.update({
            "BUILD_TARGETS_SCRIPT": str(targets), "LINT_CONFIG_READER": str(reader),
            "TEST_ROUTE_EVENTS": str(self.root / "route-events"),
            "TEST_FREE_NOW_FREE": "1", "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
        })
        return self.hold_memory()

    def test_local_fmt_takes_the_token_after_a_memory_wait_leaves_for_the_mac(self) -> None:
        _ = self.prepare_memory_lint()
        process = self.start_verify("lint", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("waiting for memory", output + errors)
        self.assertIn("leaving the memory wait", output + errors)
        self.assertIn("Mac attempt ended (passed); waiting 180 s", output + errors)
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        self.assertFalse(runners[0]["token_held"])
        formats = [event for event in self.cargo_events()
                   if event["args"] == ["+nightly", "fmt", "-p", "sample"]]
        self.assertEqual(len(formats), 1)
        self.assertTrue(formats[0]["token_held"])
        self.assertNotIn("lost the cargo token", output + errors)
        self.assertFalse((self.board / "locks/cargo.d").exists())

    def test_a_foreign_mac_block_keeps_lint_in_the_memory_wait_until_natedev_is_ready(self) -> None:
        with mock.patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "mac_test"), *sys.path]):
            from scripts.mac_test import mac_test
        meminfo = self.prepare_memory_lint()
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        block = mac_test.ActiveMacBlock(
            "assembly", "assembly checks", datetime.now(timezone.utc),
            datetime.now(timezone.utc) + timedelta(hours=1),
            mac_test.RecordedSession("assembly-session", "assembly"), mac_test.NO_SHOWRUNNER,
            "off_by_this_block", mac_test.FREE_MESSAGE_NOT_NEEDED)
        mac_test.write_block(state / "block.json", block)
        probe = self.root / "blocked-free-now.py"
        _ = probe.write_text(
            "import os, subprocess, sys\nfrom pathlib import Path\n"
            + "result = subprocess.run([sys.executable, os.environ['TEST_REAL_FREE_NOW'], *sys.argv[1:]], check=False)\n"
            + "Path(os.environ['TEST_BLOCK_PROBE']).write_text(str(result.returncode))\n"
            + "raise SystemExit(result.returncode)\n")
        observed = self.runner_state / "block-probe"
        process = self.start_verify("lint", "sample", "--no-cache", extra={
            "VERIFY_MAC_TEST_SCRIPT": str(probe),
            "TEST_REAL_FREE_NOW": str(Path(mac_test.__file__)), "TEST_BLOCK_PROBE": str(observed),
        })
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(observed, (process,))
        self.assertEqual(observed.read_text(), "1")
        self.assertIsNone(process.poll())
        self.assertEqual(self.runner_events(), [])
        ready = meminfo.with_suffix(".ready")
        _ = ready.write_text("MemAvailable: 67108864 kB\n")
        _ = ready.replace(meminfo)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("leaving the memory wait", output + errors)
        self.assertEqual(self.runner_events(), [])
        self.assertTrue(any(event["args"][:1] == ["mend"] for event in self.cargo_events()))
        self.assertTrue((state / "block.json").exists())

    def test_local_fmt_starts_after_mend_passes_on_the_mac(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        targets = self.root / "targets.py"
        _ = targets.write_text(TARGETS)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text(
            'lint_config_enabled() { [[ "$1" == mend || "$1" == fmt ]]; }\n'
            + 'lint_config_skip_notice() { :; }\n')
        shell = self.root / "hold-before-mend.sh"
        _ = shell.write_text('''
hold_before_mend() {
    case "$BASH_COMMAND" in
        'invoke_mend --workspace --fix '*)
            printf held > "$BUILD_HOLD_DIR/other"
            ;;
    esac
}
trap hold_before_mend DEBUG
''')
        self.write_executable(self.runner, RUNNER_STUB.replace(
            'raise SystemExit(runner_status)',
            'for hold in Path(os.environ["BUILD_HOLD_DIR"]).iterdir():\n'
            + '    hold.unlink()\n'
            + 'raise SystemExit(runner_status)'))
        result = self.verify("lint", "sample", "--no-cache", extra={
            "BUILD_TARGETS_SCRIPT": str(targets), "BASH_ENV": str(shell),
            "TEST_ROUTE_EVENTS": str(self.root / "route-events"),
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
        })
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        arguments = runners[0]["args"]
        self.assertEqual(arguments[arguments.index("--verb") + 1], "mend-check")
        self.assertFalse(runners[0]["token_held"])
        formats = [event for event in self.cargo_events()
                   if event["args"] == ["+nightly", "fmt", "-p", "sample"]]
        self.assertEqual(len(formats), 1)
        self.assertTrue(formats[0]["token_held"])
        self.assertNotIn("lost the cargo token", result.stderr)
        self.assertFalse((self.board / "locks/cargo.d").exists())


if __name__ == "__main__":
    _ = unittest.main()
