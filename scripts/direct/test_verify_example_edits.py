"""Exercise real Bash example gating and pass recording with private Cargo tools."""

from __future__ import annotations

import os
from pathlib import Path
import unittest

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


class ExampleEditTests(SeededExampleWorkspaceFixture):
    def prepare_edit_stub(self, before: str, after: str) -> None:
        control = self.root / "edit-order"
        control.mkdir()
        order = control / "order.py"
        _ = order.write_text('''
from pathlib import Path
import sys
from scripts.direct.wait_for_test_state import wait_for_file

wait_for_file(Path(sys.argv[1]))
''')
        cargo = self.root / "stubs/cargo"
        edit = '    printf "// changed during the test run\\n" >> "$PACKAGE_ROOT/${EDIT_DURING_TEST_PATH:-src/lib.rs}"\n'
        _ = cargo.write_text(cargo.read_text().replace(edit, before + edit + after))
        self.environment.update({
            "TEST_EDIT_CONTROL": str(control), "TEST_EDIT_ORDER": str(order),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        })

    def prepare_edit_order(self) -> None:
        self.prepare_edit_stub('    python3 "$TEST_EDIT_ORDER" "$TEST_EDIT_CONTROL/watch-read"\n', "")
        control = self.root / "edit-order"
        _ = (control / "sitecustomize.py").write_text('''
import os
from pathlib import Path
import sys
import time

if Path(sys.argv[0]).name == "tree_watch.py" and not sys.argv[1].startswith("--"):
    real_sleep = time.sleep
    def wait_after_read(interval: float) -> None:
        (Path(os.environ["TEST_EDIT_CONTROL"]) / "watch-read").touch()
        real_sleep(interval)
    time.sleep = wait_after_read
''')
        self.environment.update({
            "VERIFY_TREE_WATCH_S": "3600",
            "PYTHONPATH": str(control) + os.pathsep + self.environment["PYTHONPATH"],
        })

    def test_a_write_to_the_session_folder_in_the_worktree_keeps_the_pass(self) -> None:
        _ = self.package()
        _ = (self.root / "session" / "board.log").write_text("")
        self.environment.update({
            "TREE_DIRTY": "1", "TREE_DIRTY_PATHS": "src/lib.rs session/board.log",
            "EDIT_DURING_TEST": "1", "EDIT_DURING_TEST_PATH": "session/board.log",
        })
        result = self.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("the tree changed while test ran", result.stdout)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)

    def test_edit_during_the_test_run_records_no_pass(self) -> None:
        _ = self.package()
        self.prepare_edit_order()
        self.environment.update({"TREE_DIRTY": "1", "EDIT_DURING_TEST": "1"})
        result = self.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the tree changed while test ran, so this result was not recorded", result.stdout)
        self.assertEqual(list((self.root / "session" / "verify_cache").glob("*.pass")), [])

        self.environment["EDIT_DURING_TEST"] = ""
        result = self.verify("test", "sample")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("PASS (recorded)", result.stdout)
        self.assertEqual(len(list((self.root / "session" / "verify_cache").glob("*.pass"))), 1)

    def test_a_watched_edit_cancels_the_test_run_and_records_no_pass(self) -> None:
        _ = self.package()
        self.prepare_edit_stub("", '    python3 "$TEST_EDIT_ORDER" "$TEST_EDIT_CONTROL/cargo-finish"\n')
        self.environment["VERIFY_TREE_WATCH_S"] = "0.01"
        self.environment.update({"TREE_DIRTY": "1", "EDIT_DURING_TEST": "1"})
        result = self.verify("test", "sample")
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        self.assertIn("cancelled — the tree changed while test ran (src/lib.rs)", result.stderr)
        self.assertEqual(list((self.root / "session" / "verify_cache").glob("*.pass")), [])


if __name__ == "__main__":
    _ = unittest.main()
