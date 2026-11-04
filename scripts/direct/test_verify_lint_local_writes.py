"""Keep lint repairs in the original tree while natedev has a hold."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast
import unittest

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture


class VerifyLintLocalWritesTests(VerifyStepRoutesFixture):
    def hold_before_repair(self, tool: str) -> Path:
        (Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "other").unlink()
        shell = self.fixture.root / "hold-before-repair.sh"
        _ = shell.write_text('''
hold_before_repair() {
    case "$BASH_COMMAND" in
        'invoke_mend --workspace --fix '*|'invoke_clippy_fix --workspace '*)
            printf held > "$BUILD_HOLD_DIR/other"
            ;;
    esac
}
trap hold_before_repair DEBUG
''')
        self.fixture.environment["BASH_ENV"] = str(shell)
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text().replace('arguments = sys.argv[1:]', '''arguments = sys.argv[1:]
repair = Path(os.environ["PACKAGE_ROOT"]) / "local-repair"
if arguments[:1] == ["''' + tool + '''"]:
    if "--fix" in arguments:
        repair.write_text("fixed locally")
        repair.with_suffix(".hooks").write_text(json.dumps({
            name: os.environ.get(name) for name in ("LINT_QUEUE_LEAVE_HOOK", "BUILDLOG_MAC_IDLE_HOOK")
        }))
    elif "''' + tool + '''" == "clippy" and not repair.exists():
        raise SystemExit(101)
''')
        _ = cargo.write_text(source)
        return self.fixture.root / "local-repair"

    def assert_local_repair(self, tool: str) -> None:
        repair = self.hold_before_repair(tool)
        result = self.run_verify("lint", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(repair.exists(), result.stdout + result.stderr)
        self.assertEqual(repair.read_text(), "fixed locally")
        self.assertEqual(cast(dict[str, str], json.loads(repair.with_suffix(".hooks").read_text())), {
            "LINT_QUEUE_LEAVE_HOOK": "false", "BUILDLOG_MAC_IDLE_HOOK": "false",
        })
        commands = [cast(dict[str, object], json.loads(line))["command"]
                    for line in self.events.read_text().splitlines()]
        self.assertIn("wait-local", commands)
        calls = [event["args"] for event in self.fixture.cargo_events()]
        self.assertTrue(any(words[:1] == [tool] and "--fix" in words for words in calls), calls)
        self.assertTrue(any("fmt" in words for words in calls), calls)
        for row in self.mac_rows():
            self.assertNotIn("--fix", cast(list[str], row["args"]))
        self.assert_removed()

    def test_mend_fix_waits_for_natedev_and_changes_the_local_tree(self) -> None:
        self.assert_local_repair("mend")

    def test_clippy_fix_waits_for_natedev_and_changes_the_local_tree(self) -> None:
        self.assert_local_repair("clippy")


if __name__ == "__main__":
    _ = unittest.main()
