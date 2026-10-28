"""Hold decisions use only temporary holder and release directories."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hold_applies
import build_hold
import build_targets

SCRIPT = Path(__file__).with_name("hold_applies.py")
REFUSAL = ("natedev builds are held by builder (for app test, release eta unknown). "
           "Keep working, writes included. Run tests with verify.sh test: they go to the Mac. "
           "Run check, lint and builds after the hold ends.")


class TemporaryHoldTest(unittest.TestCase):
    root: Path = Path()
    holders: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.holders = self.root / "holders"
        targets = self.root / "targets.conf"
        _ = targets.write_text("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_HOLD_DIR": str(self.holders),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac"),
            "BUILD_TARGETS_CONFIG": str(targets),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "caller",
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
        }))

    def hold(self, key: str = "other", name: str = "builder") -> None:
        self.holders.mkdir(exist_ok=True)
        _ = (self.holders / key).write_text(json.dumps({
            "holder": name, "since": "2026-10-28T12:00:00+00:00",
            "for": "app test", "release_eta": "unknown",
        }))

    def unreadable(self) -> None:
        # A file in place of the directory fails even when tests run as root.
        _ = self.holders.write_text("not a directory\n")

    def releasing(self, *keys: str) -> None:
        cycle_id = "a" * 32
        release = self.root / "release"
        cycle_dir = release / cycle_id
        cycle_dir.mkdir(parents=True)
        cycle: build_hold.HoldCycle = {
            "id": cycle_id, "opened_at": "2026-10-28T12:00:00+00:00",
            "holders": {path.name: {"since": "2026-10-28T12:00:00+00:00",
                                    "released_at": "2026-10-28T13:00:00+00:00" if path.name in keys else ""}
                        for path in self.holders.iterdir()},
            "recipients": {}, "entries": [], "release_started_at": "2026-10-28T13:00:00+00:00",
        }
        _ = (cycle_dir / "cycle.json").write_text(json.dumps(cycle))
        _ = (release / "current").write_text(cycle_id + "\n")


class HoldAppliesTest(TemporaryHoldTest):
    def assert_cli(self, status: int, refusal: str = "") -> None:
        done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, status, done.stderr)
        self.assertEqual(done.stdout, refusal + "\n" if refusal else "")
        if status == 2:
            self.assertIn("natedev hold state cannot be read:", done.stderr)
        else:
            self.assertEqual(done.stderr, "")

    def test_no_hold(self) -> None:
        self.assertEqual(hold_applies.hold_applies(), "")
        self.assert_cli(1)
        self.holders.mkdir()
        self.assert_cli(1)

    def test_caller_is_the_only_holder(self) -> None:
        self.hold("caller")
        self.assertEqual(hold_applies.hold_applies(), "")
        self.assert_cli(1)

    def test_another_session_holds(self) -> None:
        self.hold()
        self.assertEqual(hold_applies.hold_applies(), REFUSAL)
        self.assert_cli(0, REFUSAL)

    def test_another_holder_alongside_caller(self) -> None:
        self.hold("caller", "caller builder")
        self.hold()
        refusal = hold_applies.hold_applies()
        self.assertIn("caller builder (for app test, release eta unknown)", refusal)
        self.assertIn("builder (for app test, release eta unknown)", refusal)
        self.assert_cli(0, refusal)

    def test_releasing_hold_allows_builds_with_holder_file_present(self) -> None:
        self.hold()
        self.releasing("other")
        self.assertTrue((self.holders / "other").is_file())
        self.assertEqual(hold_applies.hold_applies(), "")
        self.assert_cli(1)

    def test_refusal_names_only_in_force_holders(self) -> None:
        self.hold()
        self.hold("released", "released builder")
        self.releasing("released")
        self.assertEqual(hold_applies.hold_applies(), REFUSAL)
        self.assert_cli(0, REFUSAL)

    def test_caller_is_only_in_force_holder_next_to_releasing_hold(self) -> None:
        self.hold("caller")
        self.hold("released", "released builder")
        self.releasing("released")
        self.assertEqual(hold_applies.hold_applies(), "")
        self.assert_cli(1)

    def test_unreadable_state(self) -> None:
        self.unreadable()
        with self.assertRaises(hold_applies.HoldStateReadError):
            _ = hold_applies.hold_applies()
        self.assert_cli(2)

    def test_unreadable_release_state(self) -> None:
        self.hold("caller")
        release = self.root / "release"
        release.mkdir()
        _ = (release / "current").write_text("missing-cycle\n")
        self.assert_cli(2)

    def test_uses_wait_local_decision(self) -> None:
        self.hold()
        with mock.patch.object(build_targets, "caller_holds_only_own", return_value=True) as own:
            self.assertEqual(hold_applies.hold_applies(), "")
            own.assert_called_once_with()

    def test_main_returns_diagnostic(self) -> None:
        self.unreadable()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(hold_applies.main(), 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("cannot be read", errors.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
