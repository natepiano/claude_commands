"""Keep token ownership correct between remote and local lint steps."""
from __future__ import annotations

from pathlib import Path
import unittest

from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, RUNNER_STUB
from scripts.direct.verify_step_routes_test_support import TARGETS


class VerifyTokenMacStepTests(VerifyMacOffloadFixture):
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
