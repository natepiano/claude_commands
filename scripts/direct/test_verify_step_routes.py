"""Route a live verify step from its recorded decision."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest
from typing import cast, override

from scripts.direct import test_verify_mac_offload as fixtures


TARGETS = r'''
import json
import os
from pathlib import Path
import sys

command = sys.argv[1]
holders = Path(os.environ["BUILD_HOLD_DIR"])
with Path(os.environ["TEST_ROUTE_EVENTS"]).open("a") as output:
    output.write(json.dumps({"command": command, "args": sys.argv[2:], "call_id": os.environ.get("BUILDLOG_CALL_ID", "")}) + "\n")
if command == "eligible":
    allowed = os.environ.get("TEST_MAC_ALLOWED", "1") == "1"
    print("mac_allowed" if allowed else "natedev_only makes natedev artifacts")
    print("natedev\teligible\t-\t-")
    if allowed:
        print("mac\teligible\t-\t" + os.environ["MAC_TEST_CONFIG"])
elif command in ("register-local", "check-local"):
    raise SystemExit(10 if any(holders.iterdir()) else 0)
elif command == "wait-local":
    for path in holders.iterdir():
        path.unlink()
    print("test hold released", file=sys.stderr)
'''

RUNNER = r'''
import json
import os
from pathlib import Path
import sys

words = sys.argv[1:]
call_id = words[words.index("--call-id") + 1]
record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (call_id + ".json")).read_text())
with Path(os.environ["TEST_MAC_EVENTS"]).open("a") as output:
    output.write(json.dumps({"args": words, "record": record}) + "\n")
Path(words[words.index("--result") + 1]).write_text("mac=passed\nseconds=0\n")
'''


class VerifyStepRoutesTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: fixtures.VerifyMacOffloadTests = fixtures.VerifyMacOffloadTests("runTest")
        self.events: Path = Path()
        self.mac_events: Path = Path()

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.clean_fixture)
        root = self.fixture.root
        self.events = root / "route-events"
        self.mac_events = root / "mac-events"
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        _ = (holders / "other").write_text("test hold")
        targets = root / "targets.py"
        runner = root / "mac-runner.py"
        _ = targets.write_text(TARGETS)
        _ = runner.write_text(RUNNER)
        self.fixture.environment.update({
            "BUILD_TARGETS_SCRIPT": str(targets), "VERIFY_MAC_RUNNER": str(runner),
            "TEST_ROUTE_EVENTS": str(self.events), "TEST_MAC_EVENTS": str(self.mac_events),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "UNIT_TEAM_ROLE": "",
            "CLAUDE_CODE_SESSION_ID": "", "TEST_MAC_ALLOWED": "1", "LINT_SKIP_SWEEP": "1",
        })
        reader = root / "enabled.sh"
        _ = reader.write_text("lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n")
        self.fixture.environment["LINT_CONFIG_READER"] = str(reader)
        self.fixture.write_executable(self.fixture.bin_directory / "taplo", 'raise SystemExit(0)\n')

    def clean_fixture(self) -> None:
        self.assertTrue(self.fixture.doCleanups())

    def run_verify(self, *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(fixtures.VERIFY), *words], cwd=self.fixture.root,
                              env=self.fixture.environment, text=True, capture_output=True,
                              check=False, timeout=fixtures.HANG_GUARD_S)

    def mac_rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.mac_events.read_text().splitlines()] if self.mac_events.exists() else []

    def assert_removed(self) -> None:
        self.assertEqual(list(Path(self.fixture.environment["VERIFY_STEPS_DIR"]).glob("*.json")), [])

    def test_check_uses_mac_during_hold_and_removes_record(self) -> None:
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.mac_rows()
        self.assertEqual(len(rows), 1)
        record = cast(dict[str, object], rows[0]["record"])
        self.assertEqual((record["kind"], record["state"]), ("check", "on_mac"))
        self.assertEqual(record["runs_on"], {"kind": "mac_allowed"})
        self.assertEqual([event for event in self.fixture.cargo_events()
                          if event["args"][:1] != ["metadata"]], [])
        decisions = [cast(dict[str, object], json.loads(line)) for line in self.events.read_text().splitlines()
                     if cast(dict[str, object], json.loads(line))["command"] == "eligible"]
        self.assertTrue(decisions)
        self.assertTrue(all(row["call_id"] == "" for row in decisions))
        self.assert_removed()

    def test_lint_formats_locally_and_judges_on_mac_without_fix_passes(self) -> None:
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.mac_rows()
        verbs: list[str] = []
        for row in rows:
            words = cast(list[str], row["args"])
            verbs.append(words[words.index("--verb") + 1])
            self.assertNotIn("--fix", words)
        self.assertEqual(verbs, ["mend-check", "clippy"])
        local = [event["args"] for event in self.fixture.cargo_events() if event["args"][:1] != ["metadata"]]
        self.assertTrue(any("fmt" in words for words in local), local)
        self.assertFalse(any("mend" in words or "clippy" in words for words in local), local)
        self.assert_removed()

    def test_new_hold_during_formatting_keeps_noncompiling_work_protected(self) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if "fmt" in arguments:
    record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
    (Path(os.environ["PACKAGE_ROOT"]) / "fmt-state").write_text(record["state"])
    (Path(os.environ["BUILD_HOLD_DIR"]) / "other").write_text("new hold")
''')
        _ = cargo.write_text(source)
        self.fixture.write_executable(self.fixture.bin_directory / "taplo", '''
import json
import os
from pathlib import Path
record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
(Path(os.environ["PACKAGE_ROOT"]) / "taplo-state").write_text(record["state"])
''')
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.fixture.root / "fmt-state").read_text(), "local_noncompiling")
        self.assertEqual((self.fixture.root / "taplo-state").read_text(), "local_noncompiling")
        self.assertTrue(self.mac_rows())
        self.assert_removed()

    def hold_after_local_mend(self, status: int) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        self.fixture.environment["TEST_MEND_STATUS"] = str(status)
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
root = Path(os.environ["PACKAGE_ROOT"])
finished = root / "mend-finished"
if arguments[:1] == ["mend"]:
    record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
    (root / "mend-state").write_text(record["state"])
    finished.touch()
    raise SystemExit(int(os.environ["TEST_MEND_STATUS"]))
if finished.exists() and not Path(os.environ["TEST_MAC_EVENTS"]).exists() and (arguments[:1] == ["metadata"] or arguments == ["+nightly", "fmt", "--version"]):
    record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
    (Path(os.environ["BUILD_HOLD_DIR"]) / "other").write_text("hold after mend")
    with (root / "between-steps").open("a") as output:
        output.write(json.dumps({"args": arguments, "state": record["state"]}) + "\\n")
    if record["state"] != "local_noncompiling":
        raise SystemExit(143)
''')
        _ = cargo.write_text(source)

    def assert_lint_helpers_protected(self) -> None:
        self.assertEqual((self.fixture.root / "mend-state").read_text(), "on_natedev")
        helpers = [cast(dict[str, object], json.loads(line)) for line in
                   (self.fixture.root / "between-steps").read_text().splitlines()]
        self.assertTrue(any(row["args"] == ["+nightly", "fmt", "--version"] for row in helpers))
        self.assertTrue(any(cast(list[str], row["args"])[:1] == ["metadata"] for row in helpers))
        self.assertTrue(all(row["state"] == "local_noncompiling" for row in helpers), helpers)
        self.assertTrue(any("clippy" in cast(list[str], row["args"]) for row in self.mac_rows()))
        self.assert_removed()

    def test_hold_after_local_fix_keeps_fmt_probe_and_metadata_running(self) -> None:
        self.hold_after_local_mend(0)
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_lint_helpers_protected()

    def test_hold_after_failed_local_judgment_keeps_helpers_and_failure_status(self) -> None:
        self.hold_after_local_mend(7)
        result = self.run_verify("lint", "--workspace", "--check")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("style-lint: rustfmt passed", result.stdout)
        self.assertIn("style-lint: clippy passed", result.stdout)
        self.assert_lint_helpers_protected()

    def test_declined_lost_and_invalid_mac_results_return_to_waiting_mac(self) -> None:
        for answer in ("mac=declined\nreason=busy\n", "mac=lost\n", "invalid\n"):
            with self.subTest(answer=answer):
                root = self.fixture.root
                holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
                _ = (holders / "other").write_text("hold")
                observed = root / "pending-state"
                observed.unlink(missing_ok=True)
                marker = root / "mac-returned"
                marker.unlink(missing_ok=True)
                runner = Path(self.fixture.environment["VERIFY_MAC_RUNNER"])
                _ = runner.write_text(RUNNER.replace('"mac=passed\\nseconds=0\\n"', repr(answer))
                                      + '\n(Path(os.environ["PACKAGE_ROOT"]) / "mac-returned").touch()\n')
                targets = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
                _ = targets.write_text(TARGETS.replace(
                    'raise SystemExit(10 if any(holders.iterdir()) else 0)', '''
    marker = Path(os.environ["PACKAGE_ROOT"]) / "mac-returned"
    if marker.exists():
        record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
        (Path(os.environ["PACKAGE_ROOT"]) / "pending-state").write_text(record["state"])
        for path in holders.iterdir():
            path.unlink()
    raise SystemExit(10 if any(holders.iterdir()) else 0)'''))
                result = self.run_verify("test", "sample")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(observed.read_text(), "waiting_mac")
                self.assertTrue(any(event["args"][:2] == ["nextest", "run"] for event in self.fixture.cargo_events()))
                self.assert_removed()

    def test_natedev_only_waits_for_release_before_running(self) -> None:
        self.fixture.environment["TEST_MAC_ALLOWED"] = "0"
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = [cast(dict[str, object], json.loads(line))["command"] for line in self.events.read_text().splitlines()]
        self.assertIn("wait-local", commands)
        self.assertEqual(commands.count("eligible"), 1)
        self.assertEqual(self.mac_rows(), [])
        self.assertTrue(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))
        self.assert_removed()

    def test_stopped_local_check_is_routed_again(self) -> None:
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        (holders / "other").unlink()
        original = (self.fixture.bin_directory / "cargo").read_text()
        stub = original.replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if arguments[:1] == ["check"]:
    record = json.loads((Path(os.environ["VERIFY_STEPS_DIR"]) / (os.environ["BUILDLOG_CALL_ID"] + ".json")).read_text())
    (Path(os.environ["PACKAGE_ROOT"]) / "compiling-state").write_text(record["state"])
    (Path(os.environ["BUILD_HOLD_DIR"]) / "other").write_text("new hold")
    raise SystemExit(143)
''')
        _ = (self.fixture.bin_directory / "cargo").write_text(stub)
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hold stopped this step", result.stderr)
        self.assertEqual((self.fixture.root / "compiling-state").read_text(), "on_natedev")
        self.assertEqual(len(self.mac_rows()), 1)
        self.assert_removed()

    def test_unreadable_record_refuses_before_local_or_mac_compile(self) -> None:
        script = Path(self.fixture.environment["BUILD_TARGETS_SCRIPT"])
        # Damage the record in a check that still reports the active hold.
        source = TARGETS.replace('raise SystemExit(10 if any(holders.iterdir()) else 0)', '''
    if command == "register-local":
        for record in Path(os.environ["VERIFY_STEPS_DIR"]).glob("*.json"):
            record.write_text("{")
    raise SystemExit(10 if any(holders.iterdir()) else 0)''')
        _ = script.write_text(source)
        result = self.run_verify("check", "sample")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("step refused", result.stderr)
        self.assertEqual(self.mac_rows(), [])
        self.assertFalse(any(event["args"][:1] == ["check"] for event in self.fixture.cargo_events()))
        self.assert_removed()

    def test_recorded_stop_after_release_runs_the_local_step_again(self) -> None:
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        (holders / "other").unlink()
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
if arguments[:1] == ["check"]:
    import socket
    first = Path(os.environ["PACKAGE_ROOT"]) / "first-check"
    if not first.exists():
        first.write_text("stopped")
        proc = Path("/proc")
        start = (proc / str(os.getpid()) / "stat").read_text().rsplit(")", 1)[1].split()[19]
        log = Path(os.environ["BUILDLOG_DIR"]) / socket.gethostname().split(".")[0]
        log.mkdir(parents=True, exist_ok=True)
        (log / "hold-builds-test.jsonl").write_text(json.dumps(dict(
            kind="hold_build", action="stopped", call_id=os.environ["BUILDLOG_CALL_ID"],
            boot_id=(proc / "sys/kernel/random/boot_id").read_text().strip(),
            pid=os.getpid(), proc_start=start, id="stopped-check")) + "\\n")
        raise SystemExit(143)
''')
        _ = cargo.write_text(source)
        result = self.run_verify("check", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hold stopped this step", result.stderr)
        self.assertEqual(self.mac_rows(), [])
        self.assertEqual(len([event for event in self.fixture.cargo_events() if event["args"][:1] == ["check"]]), 1)
        self.assert_removed()


if __name__ == "__main__":
    _ = unittest.main()
