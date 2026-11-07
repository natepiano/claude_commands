#!/usr/bin/env python3
"""Reading config/dailies.conf into one production's counts."""
from __future__ import annotations

import os
from dataclasses import replace
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

from scripts.production import dailies_counts
from scripts.production.dailies_counts import CountsFailure, production_counts
from scripts.production import session_staff

LEDGER_RULE = r"— (proposed|deferred)$"


class DailiesCountsTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    owner: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]
    config: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "dailies.conf"
        self.enterContext(mock.patch.dict(os.environ, {"DAILIES_COUNTS_CONFIG": str(self.config),
                                                       "HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr"}))

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))

        owner = fixture_member("showrunner", name="count-owner")
        self.owner = staff.update_current_session(owner.current_session,
            staff.native_id(fixture_runtime_id("counts-current")))

    def configure(self, *lines: str) -> None:
        _ = self.config.write_text("# a comment\n\n" + "\n".join(line.replace("fixed-owner-id.", f"{self.owner.id}.") for line in lines) + "\n", encoding="utf-8")

    def test_rename_and_clear_keep_the_same_count_selection(self) -> None:
        self.configure("fixed-owner-id.files=files ~/*.md")
        _ = (self.root / "one.md").write_text("one")
        before = self.config.read_bytes(), self.config.stat().st_mtime_ns
        _ = staff.rename(self.owner.id, "renamed-count-owner")
        _ = staff.update_current_session(self.owner.current_session,
                                        staff.native_id(fixture_runtime_id("counts-clear")))
        self.assertEqual(production_counts(self.owner.id), [{"label": "files", "count": 1}])
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns), before)
        self.assertEqual(production_counts(self.owner.current_session), [])

    def test_tracked_counts_work_with_only_the_local_machine_owner(self) -> None:
        config = Path(dailies_counts.__file__).resolve().parents[2] / "config/dailies.conf"
        before = config.read_bytes(), config.stat().st_mtime_ns
        staff_file = Path(os.environ["STAFF_FILE"])
        before_staff = staff_file.read_bytes()
        owners = {
            "natedev": staff.StaffId("96b016bf-87cf-4a19-af7a-77425852c2e2"),
            "mac": staff.StaffId("439f9cfc-4aef-41ce-931a-c7abac39327e"),
        }
        for machine, identity in owners.items():
            with self.subTest(machine=machine):
                home = self.root / machine
                home.mkdir()
                if machine == "natedev":
                    for folder in ("nightly-review", "plan-backlog"):
                        ledger = home / ".local/state" / folder / "ledger.md"
                        ledger.parent.mkdir(parents=True)
                        _ = ledger.write_text("one — proposed\ntwo — deferred\nthree — accepted\n")
                    expected = [{"label": "nightly queue", "count": 2}, {"label": "proposals", "count": 2}]
                else:
                    issues = home / "rust/hanadocs/issues"
                    issues.mkdir(parents=True)
                    _ = (issues / "proposal.md").write_text("---\nstatus: open\ntags: [proposal]\n---\n")
                    expected = [{"label": "proposals", "count": 1}]
                row = replace(self.owner, id=identity, name=machine)
                record = staff.StaffSnapshot((row,))
                self.assertNotEqual(row.id, row.current_session)
                self.assertEqual(len(record.rows), 1)
                with (mock.patch.dict(os.environ, {"HOME": str(home), "DAILIES_COUNTS_CONFIG": str(config)}),
                      mock.patch.object(staff, "snapshot", return_value=record),
                      mock.patch.object(session_staff, "member_sources",
                                        side_effect=AssertionError("counts must not read timer evidence"))):
                    self.assertEqual(production_counts(staff.snapshot().rows[0].id), expected)
                self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns), before)
                self.assertEqual(staff_file.read_bytes(), before_staff)

    def test_counts_matching_lines_and_files_for_this_production_only(self) -> None:
        _ = (self.root / "ledger.md").write_text(
            "- a — proposed\n- b — accepted 2026-10-09\n- c — deferred\n", encoding="utf-8")
        proposals = self.root / "proposals"
        proposals.mkdir()
        for name in ("one.md", "two.md", "notes.txt"):
            _ = (proposals / name).write_text("", encoding="utf-8")
        (proposals / "folder.md").mkdir()
        self.configure(f"fixed-owner-id.nightly-queue=lines ~/ledger.md {LEDGER_RULE}",
                       "fixed-owner-id.proposals=files ~/proposals/*.md",
                       f"other.queue=lines ~/ledger.md {LEDGER_RULE}")
        self.assertEqual(production_counts(self.owner.id), [{"label": "nightly queue", "count": 2},
                                                     {"label": "proposals", "count": 2}])

    def test_counts_open_issues_with_the_tag_directly_in_the_folder(self) -> None:
        issues = self.root / "issues"
        (issues / "a proposal").mkdir(parents=True)
        for name, fields in (("open block", "status: open\ntags:\n  - issue\n  - proposal"),
                             ("open inline", 'status: "open"\ntags: [issue, proposal]'),
                             ("closed", "status: closed\ntags: [issue, proposal]"),
                             ("untagged", "status: open\ntags: [issue]"),
                             ("a proposal/notes", "status: open\ntags: [issue, proposal]")):
            _ = (issues / f"{name}.md").write_text(f"---\n{fields}\n---\nBody.\n", encoding="utf-8")
        self.configure("fixed-owner-id.proposals=issues ~/issues proposal", "fixed-owner-id.issues=issues ~/issues issue")
        self.assertEqual(production_counts(self.owner.id), [{"label": "proposals", "count": 2},
                                                     {"label": "issues", "count": 3}])

    def test_a_missing_source_reads_as_not_found(self) -> None:
        self.configure(f"fixed-owner-id.queue=lines ~/absent.md {LEDGER_RULE}", "fixed-owner-id.proposals=files ~/absent/*.md",
                       "fixed-owner-id.open=issues ~/absent proposal")
        self.assertEqual(production_counts(self.owner.id), [{"label": "queue", "not_found": True},
                                                     {"label": "proposals", "not_found": True},
                                                     {"label": "open", "not_found": True}])

    def test_a_production_without_lines_or_a_config_has_no_counts(self) -> None:
        self.assertEqual(production_counts(self.owner.id), [])
        self.configure("other.proposals=files ~/*.md")
        self.assertEqual(production_counts(self.owner.id), [])

    def test_a_bad_line_names_its_number(self) -> None:
        for line, message in (("fixed-owner-id.queue", "expected <staff id>.<label>"),
                              ("fixed-owner-id.Queue=files ~/*.md", "expected <staff id>.<label>"),
                              ("fixed-owner-id.queue=lines ~/ledger.md", "lines needs a file and a regex"),
                              ("fixed-owner-id.queue=lines ~/ledger.md (", "bad regex"),
                              ("fixed-owner-id.queue=files", "files needs a glob"),
                              ("fixed-owner-id.queue=issues ~/issues", "issues needs a folder and a tag"),
                              ("fixed-owner-id.queue=issues ~/issues two tags", "issues needs a folder and a tag"),
                              ("fixed-owner-id.queue=words ~/x", "the count kind is lines, files or issues, not words")):
            with self.subTest(line=line):
                self.configure(line)
                with self.assertRaisesRegex(CountsFailure, f"dailies.conf line 3: {message}"):
                    _ = production_counts(self.owner.id)


if __name__ == "__main__":
    _ = unittest.main()
