"""Unit session names, restart records and rename journal completion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast, final
from unittest.mock import patch
import rename_unit
import add_unit
from scripts.production.cli_fixture import parse_arguments
if TYPE_CHECKING:
    from scripts.shutdown import shutdown_record
else:
    import shutdown_record

sys.path.insert(0, str(Path(__file__).resolve().parent))



import fake_showrunner
import fake_unit_host

from scripts.production.rename_unit_test_support import UnitRenameFixture


@final
class UnitRenameSessionsTests(UnitRenameFixture):

    def test_recordless_rename_relaunch_is_a_valid_add_unit_request(self) -> None:
        rename = rename_unit.plan("demo", "alpha", "gamma", self.root / "scratch")
        rename.session_id = "sid-alpha"
        for mode in ("docs/plans/alpha.md", "standby", "permanent"):
            with self.subTest(mode=mode):
                text = self.doc.read_text().replace("alpha-unit | docs/plans/alpha.md", "gamma-unit | " + mode)
                self.write(self.doc, text)
                def launch(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    options = parse_arguments(add_unit.main, command[2:])
                    self.assertIsNotNone(options)
                    assert options is not None
                    request = add_unit.launch_request(options)
                    self.assertIsInstance(request.session, add_unit.ResumedSession)
                    self.assertEqual(request.unit_id, "gamma-unit")
                    return subprocess.CompletedProcess(command, 0, "", "")
                with (patch.object(shutdown_record, "find_session", return_value=None),
                      patch.object(subprocess, "run", side_effect=launch),
                      patch.object(rename_unit, "_await_live") as observed):
                    rename_unit._relaunch(rename, lambda: None)  # pyright: ignore[reportPrivateUsage]
                observed.assert_called_once_with(rename)
                self.write(self.doc, text.replace("gamma-unit | " + mode, "alpha-unit | docs/plans/alpha.md"))

    def moved_unit_with_leftover_marks(self) -> None:
        _ = self.seed_record()
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        fake_unit_host.write(self.env, [
            fake_unit_host.HostedUnit("old", "w3:p3Y", {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}),
            fake_unit_host.HostedUnit("moved", "w4:p1", {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "gamma-unit"}, label="gamma")])
        held = fake_showrunner.write_session(self.root / "state/sessions", "gamma", "sid-alpha")
        self.addCleanup(held.close)
        fake_unit_host.place_process(self.env, os.getpid(), "w4:p1")
        fake_unit_host.process(self.env, 991030, 991000, ["python3", "-m", "unittest"],
                               {"HERDR_PANE_ID": "w3:p3Y", "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})

    def test_verify_ignores_leftover_process_marks(self) -> None:
        self.moved_unit_with_leftover_marks()
        self.assert_ok(self.rename("verify"))

    def test_verify_names_the_live_holder_of_a_former_mark(self) -> None:
        self.moved_unit_with_leftover_marks()
        pid = fake_unit_host.live_pid(self)
        old = fake_showrunner.write_session(self.root / "state/sessions", "old", "sid-old", pid)
        self.addCleanup(old.close)
        fake_unit_host.place_process(self.env, pid, "w3:p3Y")
        self.assert_refused(self.rename("verify"),
                            f"a session still carries the mark alpha-unit: pid {pid} (claude) in herdr:old:w3:p3Y")

    def test_a_stopped_unit_relaunches_from_its_rewritten_record(self) -> None:
        log = self.seed_record()
        result = self.rename("apply")
        # The fake restart starts nothing, so verify finds the unit not running and holds the journal open.
        self.assert_refused(result, "gamma-unit is not running")
        self.assertEqual((self.journal()["step"], self.journal()["claimed"]), (10, False))
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["restart", "--only", "sid-alpha", "--here"]])
        saved = self.record()
        session = cast(dict[str, object], saved["session"])
        host = cast(dict[str, object], session["host"])
        new = self.rust / "claude-demo-gamma"
        self.assertEqual((host["unit"], host["session_name"], session["name"], session["cwd"]),
                         ("gamma-unit", "demo-gamma", "gamma", f"{new}/sub"))
        self.assertEqual(cast(dict[str, object], host["plan"])["path"], f"{new}/docs/plans/alpha.md")
        self.assertIn(f"you are now gamma-unit, in worktree {new} on branch demo-gamma, and your plan is at "
                      + f"{new}/docs/plans/alpha.md", cast(str, saved["renamed"]))
        # Each paused Codex seat resumes its own thread in the new worktree, and its resume prompt says so.
        seat = cast(list[dict[str, object]], saved["codex_seats"])[0]
        self.assertEqual(cast(list[str], seat["launcher_command"])[5], str(new))
        self.assertEqual((self.root / "runs/r1/impl.resume-prompt.md").read_text(),
                         "Continue the task you were on.\nWhile you were paused the showrunner renamed your unit: "
                         + f"your worktree is now {new} on branch demo-gamma. {self.rust / 'claude-demo-alpha'} no "
                         + f"longer exists; wherever your instructions name it, use {new}.\n")

    def test_a_session_with_its_own_name_takes_the_new_name(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        self.assertEqual(cast(dict[str, object], self.record()["session"])["name"], "gamma")
        self.assertEqual(self.journal()["old_session"], "builder-report")

    def test_the_session_name_finds_the_unit_and_only_what_differs_changes(self) -> None:
        # The user names the unit by its session (builder-report); its id already is the new one.
        _ = self.seed_record(name="builder-report", host_session="demo-builder-report")
        planned = self.rename("plan", "builder-report", "alpha")
        self.assert_ok(planned)
        self.assertIn("Claude session builder-report -> alpha", planned.stdout)
        self.assertIn("already matches: the id is alpha-unit", planned.stdout)
        branches = self.git("branch", "--list")
        doc = self.doc.read_text()
        self.assert_refused(self.rename("apply", "builder-report", "alpha"), "alpha-unit is not running")
        session = cast(dict[str, object], self.record()["session"])
        self.assertEqual((session["name"], cast(dict[str, object], session["host"])["session_name"]),
                         ("alpha", "demo-alpha"))
        self.assertEqual((self.git("branch", "--list"), self.doc.read_text()), (branches, doc))
        # The relaunched unit keeps its mark alpha-unit, now under the new session name.
        _ = self.mark_live("alpha-unit", "sid-alpha")
        self.assert_ok(self.rename("apply", "builder-report", "alpha"))
        self.assertEqual(self.journal()["done"], True)

    def test_verify_fails_while_the_old_session_name_is_live(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        _ = self.mark_live("gamma-unit", "sid-alpha", name="builder-report")
        result = self.rename("apply")
        self.assert_refused(result, "gamma-unit's session is named builder-report, not gamma")
        self.assertIn("is still labelled builder-report", result.stderr)

    def test_a_verify_that_passes_closes_the_journal(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        end = self.mark_live("gamma-unit", "sid-alpha", name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit's session is named builder-report, not gamma")
        self.assertEqual(self.journal()["done"], False)
        end()
        _ = self.mark_live("gamma-unit", "sid-alpha")
        self.assert_ok(self.rename("verify"))
        self.assertEqual(self.journal()["done"], True)
        self.assert_ok(self.rename("plan", "gamma", "delta"))


if __name__ == "__main__":
    _ = unittest.main()
