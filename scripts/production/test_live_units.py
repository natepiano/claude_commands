"""List recorded unit membership with each unit's current or offline session."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import fake_showrunner
from scripts.production import fake_unit_host
from scripts.production.fake_unit_host import HostedUnit, labeled as marked
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, isolated_staff_file, unit_membership, showrunner_relationship

SCRIPT = Path(__file__).with_name("live_units.py")


class RowDocumentLiveUnitsTests(StaffTestCase):
    def test_incomplete_facts_keep_the_live_unit_and_one_named_problem(self) -> None:
        import io
        from contextlib import redirect_stderr
        from scripts.production import live_units, unit_lookup

        fixture = self.row_production()
        every = unit_lookup.LabeledProductions({}, {fixture.slug: ("member",)})
        unavailable = fixture.worktree.with_name("unavailable-member")
        for failure in ("branch", "configuration"):
            with self.subTest(failure=failure):
                if failure == "branch":
                    _ = fixture.worktree.rename(unavailable)
                else:
                    _ = unavailable.rename(fixture.worktree)
                    (fixture.worktree / ".unit.toml").unlink()
                errors = io.StringIO()
                with (redirect_stderr(errors),
                      mock.patch("scripts.production.unit_lookup.every_labeled_session", return_value=every)):
                    units = live_units.units_of_doc(fixture.doc)
                self.assertEqual([item.unit for item in units], ["member-unit"])
                self.assertEqual(len(errors.getvalue().splitlines()), 1)
                self.assertIn(fixture.unit.name, errors.getvalue())
                self.assertIn(str(fixture.worktree), errors.getvalue())
                if failure == "configuration":
                    self.assertIn(".unit.toml", errors.getvalue())

    def test_units_in_uses_the_document_join_when_the_filename_and_timer_slug_differ(self) -> None:
        from scripts.production import live_units, session_staff

        fixture = self.row_production()
        every = session_staff.LabeledProductions({}, {fixture.slug: ("member",)})
        before = self.staff_file.read_bytes()
        selected = live_units.units_in(fixture.doc, every)
        self.assertEqual([unit.unit for unit in selected], ["member-unit"])
        self.assertEqual(self.staff_file.read_bytes(), before)




class LiveUnitsTests(unittest.TestCase):
    root: Path = Path()
    timers: Path = Path()
    doc: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}
    pane_sessions: dict[str, str] = {}

    def test_retired_backfill_does_not_add_a_retained_removed_row_to_the_active_output(self) -> None:
        self.members("live")
        self.hosts({})
        before = self.run_script()
        self.assertEqual(before.returncode, 0, before.stderr)
        retired = unit_membership("show", "retired")
        _ = staff.set_worktree(retired.id, staff.RemovedWorktree("natedev", self.root / "retired"), expected=retired.worktree)
        after = self.run_script()
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(after.stdout, before.stdout)
        self.assertIsNotNone(staff.by_id(retired.id))

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.timers = self.root / "notifier"
        self.doc = self.root / "show-production.md"
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.pane_sessions = {}
        self.environment = {**os.environ, "NOTIFIER_STATE_DIR": str(self.timers),
                            "NOTIFIER_SESSIONS_DIR": str(self.records),
                            "HERDR_PANE_RECORD_DIR": str(self.root / "pane-records"),
                            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
                            "HERDR_PROC": str(self.root / "proc"),
                            "HERDR_PLATFORM": "linux",
                            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
                            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json")}
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        _ = showrunner_relationship("show")
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"stdout": "", "code": 1}}))

    def hosts(self, units: dict[str, HostedUnit]) -> None:
        for unit in units.values():
            identity = unit.marks.get("CODEX_THREAD_ID", "")
            row = staff.by_current_session(identity)
            if row is not None:
                self.pane_sessions[unit.pane] = row.current_session
        fake_unit_host.write(self.environment, list(units.values()))

    def members(self, *units: str) -> None:
        for unit in units:
            _ = unit_membership("show", unit)

    def claude(self, name: str, pane: str) -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        if pane:
            fake_unit_host.place_process(self.environment, os.getpid(), pane, exec_shell=True)
        path = Path(self.enterContext(short_socket_directory())) / f"{name}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.records / f"{name}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": self.pane_sessions[pane], "name": name,
            "messagingSocketPath": str(path), "updatedAt": 1, "tmux": f"label-at-start:@1.{pane}" if pane else ""}))

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(SCRIPT.resolve().parents[1] / "lib" / "py"), str(SCRIPT), *(arguments or ("--production", str(self.doc)))],
                              env=self.environment, capture_output=True, text=True, check=False)

    def test_offline_staff_unit_is_listed_without_a_worktree(self) -> None:
        self.members("offline")
        self.hosts({})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "offline-unit\t\tgone\t\n")

    def test_short_unit_ids_keep_their_pane_spelling(self) -> None:
        self.members("writer", "hook")
        self.hosts({"writer": marked("director", "w4:p1", "writer")})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()), [
            "hook-unit\t\tgone\t", "writer\therdr:director:w4:p1\tunknown\t",
        ])

    def test_record_membership_wins_over_a_stale_pane_mark(self) -> None:
        self.members("alpha")
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        receiving = showrunner_relationship("receiving")
        _ = staff.move(row.id, staff.ReportsTo(receiving.id))
        self.hosts({"unit": HostedUnit("director", "w4:p1", {
            "CODEX_THREAD_ID": row.current_session, "STAFF_ID": row.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "show", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha",
        })})
        old = self.run_script()
        self.assertEqual((old.returncode, old.stdout), (0, ""), old.stderr)
        self.doc = self.root / "receiving-production.md"
        new = self.run_script()
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(new.stdout, "alpha\therdr:director:w4:p1\tunknown\t\n")

    def test_each_member_is_listed_with_its_session_as_it_is_now(self) -> None:
        self.members("alpha", "beta")
        self.hosts({"$1": marked("beta-label", "w5:p1", "beta"),
                                     "$2": marked("renamed-since-launch", "w4:p1", "alpha"),
                                     "$3": HostedUnit("done-label", "w6:p1", {}),
                                     "$4": marked("other-show", "w7:p1", "gone", slug="other")})
        self.claude("renamed-since-launch", "w4:p1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The unmarked pane has no member. The other production keeps its own member.
        self.assertEqual(sorted(result.stdout.splitlines()), ["alpha\therdr:renamed-since-launch:w4:p1\tlive\trenamed-since-launch",
                                                      "beta\therdr:beta-label:w5:p1\tunknown\t"])

    def test_herdr_pane_targets_match_between_live_units_and_lookup(self) -> None:
        self.members("alpha")
        self.hosts({"$1": marked("alpha-label", "w41:p1", "alpha")})
        listed = self.run_script()
        target = subprocess.run([str(SCRIPT.resolve().parents[1] / "lib" / "py"), str(SCRIPT.with_name("unit_lookup.py")),
                                 "pane", "show", "alpha"], env=self.environment,
                                capture_output=True, text=True, check=False)
        self.assertEqual((listed.returncode, target.returncode), (0, 0), listed.stderr + target.stderr)
        self.assertEqual(listed.stdout.split("\t")[1], "herdr:alpha-label:w41:p1")
        self.assertEqual(target.stdout, "herdr:alpha-label:w41:p1\n")

    def test_a_herdr_unit_is_listed_with_its_live_claude(self) -> None:
        self.members("alpha")
        self.hosts({"native": marked("native", "w1:p1", "alpha")})
        self.claude("native-claude", "w1:p1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "alpha\therdr:native:w1:p1\tlive\tnative-claude\n")

    def test_a_prompt_written_before_the_change_still_names_the_showrunner(self) -> None:
        self.members("alpha")
        self.hosts({"$1": marked("alpha-label", "w4:p1", "alpha")})
        _ = fake_showrunner.write_timer(self.timers, "show", "director-id", "America/Los_Angeles", self.doc)
        self.addCleanup(fake_showrunner.write_session(self.records, "director", "director-id").close)
        for showrunner in ("director", "show"):
            with self.subTest(showrunner=showrunner):
                result = self.run_script(showrunner)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["alpha\therdr:alpha-label:w4:p1\tunknown\t"])

    def test_a_host_that_cannot_say_is_an_error_not_an_empty_list(self) -> None:
        self.members("alpha")
        self.environment["HERDR_BIN"] = str(self.root / "no-such-herdr")
        result = self.run_script()
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertIn("no-such-herdr", result.stderr)

    def test_a_showrunner_no_timer_names_exits_one(self) -> None:
        result = self.run_script("missing")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("no showrunner with an update timer is called missing", result.stderr)
        self.assertEqual(self.run_script().returncode, 1)
        self.assertEqual(self.run_script("--production").returncode, 2)


if __name__ == "__main__":
    _ = unittest.main()
