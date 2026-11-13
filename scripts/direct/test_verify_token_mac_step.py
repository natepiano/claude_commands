"""Keep token ownership correct between remote and local lint steps."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import unittest

from scripts.build_queue import admission_client, admission_protocol as wire, placement
from scripts.build_queue.admission_test_support import caller_admission, caller_provider_controls
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, RUNNER_STUB, VERIFY
from scripts.direct.verify_step_routes_test_support import TARGETS


class VerifyTokenMacStepTests(VerifyMacOffloadFixture):
    def prepare_claim_boundary(self) -> None:
        Path(self.environment["BUILD_HOLD_DIR"]).mkdir()
        targets = self.root / "claim-targets.py"
        _ = targets.write_text(TARGETS.replace(
            'if command == "eligible":',
            'if command in ("reserve", "release-wait"):\n'
            + '    import subprocess\n'
            + '    arguments = sys.argv[1:]\n'
            + '    if command == "reserve":\n'
            + '        package = arguments.index("--package") + 1\n'
            + '        if arguments[package] == "--workspace":\n'
            + '            arguments[package] = ""\n'
            + '    raise SystemExit(subprocess.call([sys.executable, os.environ["TEST_REAL_TARGETS"], *arguments]))\n'
            + 'if command == "eligible":'))
        reader = self.root / "claim-lint-config.sh"
        _ = reader.write_text(
            'lint_config_enabled() { [[ "$1" == mend || "$1" == fmt ]]; }\n'
            + 'lint_config_skip_notice() { :; }\n')
        direct = self.home / ".claude/scripts/direct"
        direct.unlink()
        direct.mkdir()
        for source in VERIFY.parent.iterdir():
            if source.name != "board.sh":
                (direct / source.name).symlink_to(source, target_is_directory=source.is_dir())
        observer = self.root / "observe-local-token.py"
        _ = observer.write_text('''
import os
from pathlib import Path
import sys
if sys.argv[1] == "acquire":
    records = Path(os.environ["VERIFY_STEPS_DIR"]).glob("*.json")
    with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.test_verify_step_state import read_step_row
    if any(read_step_row(path.stem)["runs_on"]["kind"] == "natedev_only" for path in records):
        (Path(os.environ["TEST_RUNNER_STATE"]) / "local-token-wait").touch()
''')
        self.write_executable(direct / "board.sh", '''#!/bin/bash
"$TEST_REAL_PYTHON" "$TEST_LOCAL_TOKEN_OBSERVER" "$@"
exec bash "$TEST_REAL_BOARD" "$@"
''')
        self.environment.update({
            "BUILD_TARGETS_SCRIPT": str(targets), "LINT_CONFIG_READER": str(reader),
            "TEST_ROUTE_EVENTS": str(self.root / "route-events"),
            "TEST_REAL_TARGETS": str(VERIFY.parents[1] / "build_targets/build_targets.py"),
            "TEST_REAL_BOARD": str(VERIFY.with_name("board.sh")),
            "TEST_FREE_NOW_FREE": "1", "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
            "TEST_PEER_PID": str(os.getpid()), "TEST_LOCAL_TOKEN_OBSERVER": str(observer),
        })

    def take_peer_token(self) -> None:
        result = subprocess.run(
            ["bash", str(VERIFY.with_name("board.sh")), "acquire", str(self.board), "peer", "cargo",
             "--pid", str(os.getpid()), "--hold", "3600"],
            env=self.environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def release_peer_token(self) -> None:
        result = subprocess.run(
            ["bash", str(VERIFY.with_name("board.sh")), "release", str(self.board), "peer", "cargo",
             "--pid", str(os.getpid())],
            env=self.environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_local_token_wait_has_no_mac_claim(self, after_mac: bool) -> None:
        self.prepare_claim_boundary()
        self.addCleanup(self.release_peer_token)
        if after_mac:
            _ = self.hold_memory()
            self.write_executable(self.runner, RUNNER_STUB.replace(
                'raise SystemExit(runner_status)',
                'import subprocess\n'
                + 'claims = list((Path(os.environ["MAC_TEST_STATE_DIR"]) / "claims").glob("*.json"))\n'
                + 'assert len(claims) == 1, claims\n'
                + '(root / "mac-stage-claim").write_text(claims[0].read_text())\n'
                + 'for hold in Path(os.environ["BUILD_HOLD_DIR"]).iterdir():\n'
                + '    hold.unlink()\n'
                + 'subprocess.run(["bash", os.environ["TEST_REAL_BOARD"], "acquire", os.environ["UNIT_RUN_DIR"], '
                + '"peer", "cargo", "--pid", os.environ["TEST_PEER_PID"], "--hold", "3600"], check=True)\n'
                + 'raise SystemExit(runner_status)'))
        else:
            self.take_peer_token()
        arguments = ("lint", *(("sample",) if not after_mac else ()), "--no-cache")
        if after_mac:
            arguments += ("--check",)
        process = self.start_verify(*arguments)
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "local-token-wait", (process,))
        self.assertIsNone(process.poll())
        self.assertEqual((self.board / "locks/cargo.d/holder_pid").read_text(), str(os.getpid()))
        self.assertEqual(len(self.runner_events()), 1 if after_mac else 0)
        if after_mac:
            self.assertTrue((self.runner_state / "mac-stage-claim").exists())
        claims = list((Path(self.environment["MAC_TEST_STATE_DIR"]) / "claims").glob("*.json"))
        self.assertEqual([(path.name, path.read_text()) for path in claims], [])
        events = self.root / "route-events"
        if after_mac:
            self.assertTrue(events.exists())
            self.assertIn('"command": "reserve"', events.read_text())
        else:
            self.assertNotIn('"command": "reserve"', events.read_text() if events.exists() else "")
        self.release_peer_token()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)

    def test_local_mend_fix_waits_for_the_token_without_a_mac_reservation(self) -> None:
        self.assert_local_token_wait_has_no_mac_claim(after_mac=False)

    def test_mac_stage_releases_its_claim_before_local_fmt_waits_for_the_token(self) -> None:
        self.assert_local_token_wait_has_no_mac_claim(after_mac=True)

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
        meminfo = self.hold_memory()
        admission = caller_admission(self, self.root, self.environment)
        controls = caller_provider_controls(self, admission)
        controls.set_host("mac", placement.TemporaryExclusion("busy"))
        reply_filter = admission.server.reply_filter

        def release_provider(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.PlacementWait):
                for path in Path(self.environment["TMPDIR"]).glob("verify-hold.*/placement-*.json.wait"):
                    notice = wire.parse_record(path.read_text(), admission_client.PlacementWaitNotice)
                    if isinstance(notice.last_reply, wire.Wait):
                        _ = controls.hosts.pop("mac", None)
            return reply_filter(reply)

        admission.server.reply_filter = release_provider
        return meminfo

    def test_local_fmt_takes_the_token_after_a_memory_wait_leaves_for_the_mac(self) -> None:
        _ = self.prepare_memory_lint()
        process = self.start_verify("lint", "--check", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("waiting for memory", output + errors)
        self.assertIn("leaving the memory wait", output + errors)
        self.assertNotIn("waiting 180 s", output + errors)
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        self.assertFalse(runners[0]["token_held"])
        formats = [event for event in self.cargo_events()
                   if event["args"] == ["+nightly", "fmt", "--all", "--check"]]
        self.assertEqual(len(formats), 1)
        self.assertTrue(formats[0]["token_held"])
        self.assertNotIn("lost the cargo token", output + errors)
        self.assertFalse((self.board / "locks/cargo.d").exists())

    def test_a_foreign_mac_block_keeps_lint_in_the_memory_wait_until_natedev_is_ready(self) -> None:
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
        observed = self.runner_state / "block-probe"
        admission = caller_admission(self, self.root, self.environment)
        provider = admission.server.host_probe

        def observe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            evidence = provider(key)
            if key.destination.provider == "mac" and isinstance(evidence, placement.FreshHostEvidence):
                pending = observed.with_suffix(".pending")
                _ = pending.write_text(str(int(not isinstance(evidence.eligibility, placement.AllowedHost))))
                _ = pending.replace(observed)
            return evidence

        admission.server.host_probe = observe
        process = self.start_verify("lint", "--check", "--no-cache")
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
        self.assertTrue(any(event["args"][:1] == ["mend"] and "--fail-on-warn" in event["args"]
                            and "--fix" not in event["args"] for event in self.cargo_events()))
        self.assertTrue((state / "block.json").exists())

    def test_local_fmt_check_starts_after_mend_check_passes_on_the_mac(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        targets = self.root / "targets.py"
        _ = targets.write_text(TARGETS)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text(
            'lint_config_enabled() { [[ "$1" == mend || "$1" == fmt ]]; }\n'
            + 'lint_config_skip_notice() { :; }\n')
        shell = self.root / "hold-before-mend-check.sh"
        _ = shell.write_text('''
hold_before_mend_check() {
    case "$BASH_COMMAND" in
        'invoke_mend --workspace --fail-on-warn '*)
            printf held > "$BUILD_HOLD_DIR/other"
            ;;
    esac
}
set -o functrace
trap hold_before_mend_check DEBUG
''')
        self.write_executable(self.runner, RUNNER_STUB.replace(
            'raise SystemExit(runner_status)',
            'for hold in Path(os.environ["BUILD_HOLD_DIR"]).iterdir():\n'
            + '    hold.unlink()\n'
            + 'raise SystemExit(runner_status)'))
        result = self.verify("lint", "--check", "--no-cache", extra={
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
                   if event["args"] == ["+nightly", "fmt", "--all", "--check"]]
        self.assertEqual(len(formats), 1)
        self.assertTrue(formats[0]["token_held"])
        self.assertNotIn("lost the cargo token", result.stderr)
        self.assertFalse((self.board / "locks/cargo.d").exists())


if __name__ == "__main__":
    _ = unittest.main()
