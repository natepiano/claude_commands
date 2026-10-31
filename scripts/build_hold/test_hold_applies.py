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
from override_guard import require_checkout

require_checkout(Path(__file__))


import hold_applies
import build_hold
import build_targets

SCRIPT = Path(__file__).with_name("hold_applies.py")
REFUSAL = ("natedev builds are held by builder (for app test, release eta unknown). "
           "Keep working, writes included. Use verify.sh: eligible steps go to the Mac; other steps wait. "
           "Raw cargo and merge gates must wait for the hold to end.")


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
            "HOME": str(self.root),
            "BUILD_HOLD_DIR": str(self.holders),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac"),
            "VERIFY_STEPS_DIR": str(self.root / "steps"),
            "VERIFY_PROC_ROOT": str(self.root / "proc"),
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


class CargoVerbTests(unittest.TestCase):
    def test_build_verbs_match_the_wrapper_and_lookup_verbs_pass(self) -> None:
        wrapper = SCRIPT.with_name("cargo").read_text()
        branch = wrapper.split('case "$subcommand" in', 1)[1].split(') ;;', 1)[0].strip()
        self.assertEqual(hold_applies.BUILDING_CARGO_VERBS, frozenset(branch.split("|")))
        for verb in hold_applies.BUILDING_CARGO_VERBS | {"metadata", "locate-project", "tree", "version", "search"}:
            with self.subTest(verb=verb):
                self.assertEqual(hold_applies.cargo_verb(("+nightly", "--config=term.color=never", verb)), verb)
        self.assertEqual(hold_applies.cargo_verb(("+stable", "-v")), "")

    def test_option_values_cannot_turn_a_lookup_into_a_build(self) -> None:
        for option in hold_applies.CARGO_OPTIONS_WITH_VALUES:
            with self.subTest(option=option):
                self.assertEqual(hold_applies.cargo_verb((option, "build", "metadata")), "metadata")
                self.assertEqual(hold_applies.cargo_verb((option, "metadata", "check")), "check")
        self.assertEqual(hold_applies.cargo_verb(("--config",)), "")


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

    def test_release_record_never_admits_while_holder_file_exists(self) -> None:
        self.hold()
        self.releasing("other")
        self.assertTrue((self.holders / "other").is_file())
        self.assertEqual(hold_applies.hold_applies(), REFUSAL)
        self.assert_cli(0, REFUSAL)

    def test_refusal_names_every_present_holder_despite_release_record(self) -> None:
        self.hold()
        self.hold("released", "released builder")
        self.releasing("released")
        refusal = hold_applies.hold_applies()
        self.assertIn("released builder", refusal)
        self.assertIn("builder (for app test", refusal)
        self.assert_cli(0, refusal)

    def test_another_present_holder_blocks_the_caller_despite_release_record(self) -> None:
        self.hold("caller")
        self.hold("released", "released builder")
        self.releasing("released")
        refusal = hold_applies.hold_applies()
        self.assertIn("released builder", refusal)
        self.assert_cli(0, refusal)

    def test_unreadable_state(self) -> None:
        self.unreadable()
        with self.assertRaises(hold_applies.HoldStateReadError):
            _ = hold_applies.hold_applies()
        self.assert_cli(2)

    def test_unreadable_release_state_does_not_change_sole_holder_admission(self) -> None:
        self.hold("caller")
        release = self.root / "release"
        release.mkdir()
        _ = (release / "current").write_text("missing-cycle\n")
        self.assert_cli(1)

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
