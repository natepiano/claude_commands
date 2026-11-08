"""Seeded tools and real Bash step-routing fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest
from typing import cast, override

from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


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
    print("any_target" if allowed else "natedev_only makes natedev artifacts")
    print("natedev\teligible\t-\t-\t-")
    if allowed:
        print("mac\teligible\t-\t" + os.environ["MAC_TEST_CONFIG"] + "\tmacos")
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
with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.test_verify_step_state import read_step_row
record = read_step_row(call_id)
with Path(os.environ["TEST_MAC_EVENTS"]).open("a") as output:
    output.write(json.dumps({"args": words, "record": record}) + "\n")
Path(words[words.index("--result") + 1]).write_text("mac=passed\nseconds=0\n")
'''


class VerifyStepRouteTools(VerifyMacOffloadFixture):
    @override
    def build_fixture(self) -> None:
        super().build_fixture()
        root = self.root
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        _ = (holders / "other").write_text("test hold")
        targets = root / "targets.py"
        runner = root / "mac-runner.py"
        _ = targets.write_text(TARGETS)
        _ = runner.write_text(RUNNER)
        self.environment.update({
            "BUILD_TARGETS_SCRIPT": str(targets), "VERIFY_MAC_RUNNER": str(runner),
            "TEST_ROUTE_EVENTS": str(root / "route-events"), "TEST_MAC_EVENTS": str(root / "mac-events"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "UNIT_TEAM_ROLE": "",
            "CLAUDE_CODE_SESSION_ID": "", "TEST_MAC_ALLOWED": "1", "LINT_SKIP_SWEEP": "1",
        })
        reader = root / "enabled.sh"
        _ = reader.write_text("lint_config_enabled() { return 0; }\nlint_config_skip_notice() { :; }\n_lint_config_raw() { :; }\n")
        self.environment["LINT_CONFIG_READER"] = str(reader)
        self.write_executable(self.bin_directory / "taplo", "import sys\nif sys.argv[1:] == ['--version']:\n    print('taplo fixture 1.0')\n")


class VerifyStepRoutesFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: VerifyStepRouteTools = VerifyStepRouteTools("runTest")
        self.events: Path = Path()
        self.mac_events: Path = Path()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        VerifyStepRouteTools.setUpClass()
        cls.addClassCleanup(VerifyStepRouteTools.doClassCleanups)

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.clean_fixture)
        self.events = self.fixture.root / "route-events"
        self.mac_events = self.fixture.root / "mac-events"

    def clean_fixture(self) -> None:
        self.assertTrue(self.fixture.doCleanups())

    def run_verify(self, *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), *words], cwd=self.fixture.root,
                              env=self.fixture.environment, text=True, capture_output=True,
                              check=False, timeout=TEST_HANG_GUARD_SECS)

    def mac_rows(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.mac_events.read_text().splitlines()] if self.mac_events.exists() else []

    def assert_removed(self) -> None:
        self.assertEqual(list(Path(self.fixture.environment["VERIFY_STEPS_DIR"]).glob("*.json")), [])

    def hold_after_local_mend(self, status: int) -> None:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        self.fixture.environment["TEST_MEND_STATUS"] = str(status)
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
root = Path(os.environ["PACKAGE_ROOT"])
finished = root / "mend-finished"
if arguments[:1] == ["mend"]:
    import sys
    with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.test_verify_step_state import read_step_row
    record = read_step_row(os.environ["BUILDLOG_CALL_ID"])
    (root / "mend-state").write_text(record["state"])
    finished.touch()
    raise SystemExit(int(os.environ["TEST_MEND_STATUS"]))
if finished.exists() and not Path(os.environ["TEST_MAC_EVENTS"]).exists() and (arguments[:1] == ["metadata"] or arguments == ["+nightly", "fmt", "--version"]):
    import sys
    with open(Path.home() / ".claude/scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.test_verify_step_state import read_step_row
    record = read_step_row(os.environ["BUILDLOG_CALL_ID"])
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
        self.assertEqual(len([event for event in self.fixture.cargo_events()
                              if event["args"][:1] == ["metadata"]]), 1)
        self.assertTrue(all(row["state"] == "local_noncompiling" for row in helpers), helpers)
        self.assertTrue(any("clippy" in cast(list[str], row["args"]) for row in self.mac_rows()))
        self.assert_removed()
