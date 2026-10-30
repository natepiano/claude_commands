"""Tests for finding a production's units by the marks on their herdr panes."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.lib.socket_test_support import short_socket_directory
import fake_unit_host
import showrunners
import unit_lookup
import herdr
import sessions
from fake_herdr import herdr_listener
from fake_unit_host import HostedUnit
from sessions import SessionRecord
from unit_lookup import ClaudeNotRunning, ClaudeUnknown, LiveClaude, UnitState

SCRIPT = Path(__file__).with_name("unit_lookup.py")


def marked(label: str, pane: str, unit: str, slug: str = "show", **extra: str) -> HostedUnit:
    return HostedUnit(label, pane, {"SHOWRUNNER_UNIT": slug, "SHOWRUNNER_UNIT_ID": unit, **extra})


class UnitLookupTests(unittest.TestCase):
    root: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.environment = {**os.environ,
                            "HOME": str(self.root / "home"),
                            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                            "NOTIFIER_SESSIONS_DIR": str(self.records),
                            "UNIT_HISTORY_DIR": str(self.root / "history"),
                            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
                            "HERDR_PROC": str(self.root / "proc"),
                            "HERDR_PLATFORM": "linux",
                            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
                            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json")}
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"stdout": "", "code": 1}}))
        self.enterContext(mock.patch.dict(os.environ, self.environment))
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))

    def hosts(self, units: dict[str, HostedUnit]) -> None:
        fake_unit_host.write(self.environment, list(units.values()))

    def renamed_production(self) -> None:
        doc = self.root / "new-production.md"
        _ = doc.write_text("# Production\n\n## Production Context\n\n- **Formerly:** `old`\n")
        instance = self.root / "notifier/showrunner-new"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:showrunner\nCHECK=zsh /scripts/production_check.sh {doc}\n")

    def process(self, pid: int, parent: int, argv: list[str], **environment: str) -> None:
        directory = self.root / "proc" / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        _ = (directory / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
        _ = (directory / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
        _ = (directory / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in environment.items()).encode())
        (directory / "cwd").symlink_to(self.root)

    def herdr_session(self, name: str = "native", pane: str = "w1:p1", unit: str = "trunk-unit") -> None:
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({
            "--session default session list --json": {"code": 0, "stdout": json.dumps({"sessions": [{
                "name": name, "running": True, "default": False, "session_dir": "/fixture", "socket_path": "/fixture/sock"
            }]})}
        }))
        self.process(991000, 0, ["herdr", "--session", name, "server"])
        herdr_listener(self.root / "proc", 991000, name, self.environment)
        self.process(os.getpid(), 991000, ["claude"], HERDR_PANE_ID=pane,
                     SHOWRUNNER_UNIT="show", SHOWRUNNER_UNIT_ID=unit)

    def claude(self, name: str, pane: str, *, updated: int = 1, session_id: str = "", pid: int | None = None) -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        pid = os.getpid() if pid is None else pid
        if pane:
            fake_unit_host.place_process(self.environment, pid, pane)
        path = Path(self.enterContext(short_socket_directory())) / f"{name}-{updated}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.records / f"{name}-{updated}.json").write_text(json.dumps({
            "pid": pid, "sessionId": session_id or f"id-{name}", "name": name,
            "messagingSocketPath": str(path), "updatedAt": updated, "tmux": f"label-at-start:@1.{pane}" if pane else ""}))

    def live_hosts(self, *panes: str) -> None:
        for pane in panes:
            self.claude(pane, pane, pid=fake_unit_host.live_pid(self))

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = self.environment
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=environment, capture_output=True,
                              text=True, check=False)


    def test_a_unit_is_found_by_its_mark_whatever_its_session_is_called_now(self) -> None:
        self.hosts({"$1": marked("renamed-since-launch", "w4:p1", "trunk-unit"),
                                     "$2": marked("another-show", "w5:p1", "trunk-unit", slug="other"),
                                     "$3": HostedUnit("unmarked", "w6:p1", {})})
        self.claude("renamed-since-launch", "w4:p1")
        found = unit_lookup.marked_units("show")
        self.assertEqual(list(found), ["trunk-unit"])
        unit = found["trunk-unit"]
        self.assertEqual((unit.place, unit.place.session), (herdr.HerdrPlace("renamed-since-launch", "w4:p1"), "renamed-since-launch"))
        assert isinstance(unit.claude, LiveClaude)
        self.assertEqual((unit.claude.name, unit.claude.session_id), ("renamed-since-launch", "id-renamed-since-launch"))

    def test_old_marked_sessions_are_found_under_both_production_names(self) -> None:
        self.renamed_production()
        self.hosts({"$1": marked("still-old", "w4:p1", "trunk-unit", slug="old")})
        self.claude("still-old", "w4:p1")
        found = unit_lookup.every_marked_unit()
        self.assertEqual(list(found.units), ["new"])
        for slug in ("old", "new"):
            with self.subTest(slug=slug):
                self.assertEqual(unit_lookup.marked_units(slug)["trunk-unit"].place, herdr.HerdrPlace("still-old", "w4:p1"))
                pane = self.run_script("pane", slug, "trunk-unit")
                self.assertEqual((pane.returncode, pane.stdout), (0, "herdr:still-old:w4:p1\n"), pane.stderr)
                listed = self.run_script("list", slug)
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertEqual(listed.stdout.split("\t")[:6],
                                 ["trunk-unit", "herdr:still-old:w4:p1", "still-old", "live", "still-old", "id-still-old"])

    def test_grouping_resolves_former_slugs_without_discovering_processes(self) -> None:
        self.renamed_production()
        unit = unit_lookup.MarkedUnit("trunk-unit", LiveClaude("report", "report-id", "/fixture.sock", 123),
                                      herdr.HerdrPlace("old-pane", "w4:p1"))
        with mock.patch.object(herdr, "herdr_processes", side_effect=AssertionError("no process scan")) as scan:
            found = unit_lookup.group_marked_units(iter([("old", unit)]))
        self.assertEqual(found.units, {"new": {"trunk-unit": unit}})
        self.assertEqual(found.faults, {})
        scan.assert_not_called()

    def test_grouping_reports_duplicate_alias_marks_and_retains_unambiguous_units(self) -> None:
        self.renamed_production()
        before = unit_lookup.MarkedUnit("trunk-unit", LiveClaude("before", "before-id", "/before.sock", 123),
                                        herdr.HerdrPlace("before", "w4:p1"))
        after = unit_lookup.MarkedUnit("trunk-unit", LiveClaude("after", "after-id", "/after.sock", 124),
                                       herdr.HerdrPlace("after", "w5:p1"))
        other = unit_lookup.MarkedUnit("other-unit", ClaudeNotRunning(), herdr.HerdrPlace("other", "w6:p1"))
        found = unit_lookup.group_marked_units([("old", before), ("new", after), ("new", other)])
        self.assertEqual(found.units, {"new": {"other-unit": other}})
        holders = "pid 123 (claude) in herdr:before:w4:p1 and pid 124 (claude) in herdr:after:w5:p1"
        self.assertEqual(found.faults, {
            "new": {"trunk-unit": f"trunk-unit: its mark is on herdr sessions {holders}"},
        })

    def test_grouping_prefers_live_claude_over_inherited_child_marks(self) -> None:
        self.renamed_production()
        director = unit_lookup.MarkedUnit("trunk-unit", LiveClaude("report", "report-id", "/report.sock", 123),
                                          herdr.HerdrPlace("director", "w4:p1"))
        detached = unit_lookup.MarkedUnit("trunk-unit", ClaudeNotRunning(), herdr.HerdrPlace("build", "w5:p1"))
        found = unit_lookup.group_marked_units([("old", detached), ("new", director)])
        self.assertEqual(found.units, {"new": {"trunk-unit": director}})
        self.assertEqual(found.faults, {})


    def test_former_and_current_marks_cannot_hide_duplicate_unit_sessions(self) -> None:
        self.renamed_production()
        self.hosts({"$1": marked("before", "w4:p1", "trunk-unit", slug="old"),
                                     "$2": marked("after", "w5:p1", "trunk-unit", slug="new")})
        self.live_hosts("w4:p1", "w5:p1")
        found = unit_lookup.every_marked_unit()
        self.assertEqual(found.units, {"new": {}})
        self.assertIn("herdr:after:w5:p1", found.faults["new"]["trunk-unit"])
        self.assertIn("herdr:before:w4:p1", found.faults["new"]["trunk-unit"])
        for slug in ("old", "new"):
            with self.subTest(slug=slug):
                with self.assertRaisesRegex(OSError, "trunk-unit: its mark is on herdr sessions"):
                    _ = unit_lookup.marked_units(slug)
                with self.assertRaisesRegex(OSError, "trunk-unit: its mark is on herdr sessions"):
                    _ = unit_lookup.marked_unit(slug, "trunk-unit")
                for arguments in (("list", slug), ("pane", slug, "trunk-unit")):
                    result = self.run_script(*arguments)
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("trunk-unit: its mark is on herdr sessions", result.stderr)

    def test_sessions_sharing_a_production_read_its_timer_once_per_discovery(self) -> None:
        self.renamed_production()
        self.hosts({
            "$1": marked("first", "w4:p1", "first-unit", slug="old"),
            "$2": marked("second", "w5:p1", "second-unit", slug="old"),
            "$3": marked("third", "w6:p1", "third-unit", slug="old"),
        })
        self.live_hosts("w4:p1", "w5:p1", "w6:p1")
        with mock.patch.object(showrunners, "_source", wraps=getattr(showrunners, "_source")) as reads:
            for expected_reads in (1, 2):
                found = unit_lookup.every_marked_unit()
                self.assertEqual(set(found.units["new"]), {"first-unit", "second-unit", "third-unit"})
                self.assertEqual(reads.call_count, expected_reads)

    def test_current_production_discovery_keeps_an_existing_timer_read_budget(self) -> None:
        self.renamed_production()
        self.hosts({"$1": marked("current", "w4:p1", "trunk-unit", slug="new")})
        self.claude("current", "w4:p1")
        with mock.patch.object(showrunners, "checked_doc", wraps=showrunners.checked_doc) as reads:
            checked = showrunners.checked_doc(self.root / "notifier/showrunner-new")
            self.assertIsInstance(checked, showrunners.CheckedDoc)
            found = unit_lookup.every_marked_unit()
            self.assertEqual(list(found.units["new"]), ["trunk-unit"])
            self.assertEqual(reads.call_count, 1, "current marks must not reread already inventoried timers")

    def test_current_production_lookup_does_not_read_timer_docs(self) -> None:
        self.renamed_production()
        self.hosts({"$1": marked("current", "w4:p1", "trunk-unit", slug="new")})
        self.claude("current", "w4:p1")
        with mock.patch.object(showrunners, "checked_doc", wraps=showrunners.checked_doc) as reads:
            found = unit_lookup.marked_units("new")
            self.assertEqual(found["trunk-unit"].place, herdr.HerdrPlace("current", "w4:p1"))
            self.assertEqual(reads.call_count, 0)

    def test_herdr_marks_are_grouped_under_the_current_production(self) -> None:
        self.renamed_production()
        self.herdr_session()
        self.claude("current", "")
        environment = self.root / "proc" / str(os.getpid()) / "environ"
        _ = environment.write_bytes(b"HERDR_PANE_ID=w1:p1\0SHOWRUNNER_UNIT=old\0SHOWRUNNER_UNIT_ID=trunk-unit")
        self.assertEqual(list(unit_lookup.every_marked_unit().units), ["new"])
        for slug in ("old", "new"):
            with self.subTest(slug=slug):
                pane = self.run_script("pane", slug, "trunk-unit")
                self.assertEqual((pane.returncode, pane.stdout), (0, "herdr:native:w1:p1\n"), pane.stderr)


    def test_a_relaunch_in_the_same_pane_is_found_from_its_newest_record(self) -> None:
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit")})
        self.claude("before", "w4:p1", updated=1, session_id="old-id")
        self.claude("after", "w4:p1", updated=2, session_id="new-id")
        claude = unit_lookup.marked_units("show")["trunk-unit"].claude
        assert isinstance(claude, LiveClaude)
        self.assertEqual((claude.name, claude.session_id), ("after", "new-id"))

    def test_a_marked_session_with_no_claude_is_still_the_units_pane(self) -> None:
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit")})
        unit = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertEqual(unit.place, herdr.HerdrPlace("trunk", "w4:p1"))
        self.assertIsInstance(unit.claude, ClaudeNotRunning)

    def test_nested_tmux_claude_does_not_join_the_outer_marked_unit(self) -> None:
        self.hosts({"trunk": marked("trunk", "w4:p1", "trunk-unit")})
        inherited = {"HERDR_PANE_ID": "w4:p1", "SHOWRUNNER_UNIT": "show",
                     "SHOWRUNNER_UNIT_ID": "trunk-unit"}
        self.process(991020, 991001, ["tmux: server"], **inherited)
        nested = {**inherited, "TMUX_PANE": "%7"}
        self.process(991021, 991020, ["zsh"], **nested)
        self.process(os.getpid(), 991021, ["claude"], **nested)
        self.claude("nested", "", updated=2)
        unit = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertEqual(unit.place, herdr.HerdrPlace("trunk", "w4:p1"))
        self.assertIsInstance(unit.claude, ClaudeNotRunning)

    def test_outer_panes_claude_is_found_despite_a_newer_nested_tmux_claude(self) -> None:
        self.hosts({"trunk": marked("trunk", "w4:p1", "trunk-unit")})
        self.claude("outer", "w4:p1", updated=1)
        nested_pid = fake_unit_host.live_pid(self)
        inherited = {"HERDR_PANE_ID": "w4:p1", "SHOWRUNNER_UNIT": "show",
                     "SHOWRUNNER_UNIT_ID": "trunk-unit"}
        self.process(991020, 991001, ["tmux: server"], **inherited)
        nested = {**inherited, "TMUX_PANE": "%7"}
        self.process(991021, 991020, ["zsh"], **nested)
        self.process(nested_pid, 991021, ["claude"], **nested)
        record = SessionRecord(pid=nested_pid, name="nested", sessionId="id-nested", updatedAt=2,
                               messagingSocketPath=str(self.root / "outer-1.sock"), tmux="", formerNames=[])
        _ = (self.records / "nested.json").write_text(json.dumps(record))
        unit = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertIsInstance(unit.claude, LiveClaude)
        assert isinstance(unit.claude, LiveClaude)
        self.assertEqual((unit.claude.pid, unit.claude.name), (os.getpid(), "outer"))

    def test_an_unreadable_record_leaves_a_unit_without_a_match_unknown_not_stopped(self) -> None:
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit"), "$2": marked("fps", "w5:p1", "fps-unit")})
        self.claude("fps", "w5:p1")
        _ = (self.records / "broken.json").write_text("{")
        found = unit_lookup.marked_units("show")
        self.assertIsInstance(found["trunk-unit"].claude, ClaudeUnknown)
        self.assertIsInstance(found["fps-unit"].claude, LiveClaude)

    def test_a_broken_record_of_an_exited_session_leaves_every_unit_stopped(self) -> None:
        # During an account move many sessions exit at once; their records are not evidence about other panes.
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit")})
        exited = subprocess.Popen(["true"])
        _ = exited.wait(timeout=TEST_HANG_GUARD_SECS)
        _ = (self.records / f"{exited.pid}.json").write_text("{")
        self.assertIsInstance(unit_lookup.marked_units("show")["trunk-unit"].claude, ClaudeNotRunning)

    def test_a_broken_record_of_a_live_session_makes_only_its_own_pane_unknown(self) -> None:
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit"), "$2": marked("fps", "w5:p1", "fps-unit")})
        pid = fake_unit_host.live_pid(self)
        fake_unit_host.place_process(self.environment, pid, "w5:p1")
        _ = (self.records / f"{pid}.json").write_text("{")
        found = unit_lookup.marked_units("show")
        self.assertIsInstance(found["trunk-unit"].claude, ClaudeNotRunning)
        self.assertEqual(found["fps-unit"].claude, ClaudeUnknown(f"the session record of pid {pid} could not be read"))


    def test_two_sessions_carrying_one_units_mark_are_an_error(self) -> None:
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit"), "$2": marked("copy", "w5:p1", "trunk-unit")})
        self.live_hosts("w4:p1", "w5:p1")
        with self.assertRaisesRegex(OSError, "trunk-unit: its mark is on herdr sessions") as caught:
            _ = unit_lookup.marked_units("show")
        for process in herdr.herdr_processes():
            if process.command == "claude":
                self.assertIn(f"pid {process.pid} (claude) in {unit_lookup.place_text(process.place)}", str(caught.exception))


    def test_two_live_claudes_with_the_same_mark_in_one_pane_are_an_error(self) -> None:
        self.hosts({"trunk": marked("trunk", "w4:p1", "trunk-unit")})
        self.claude("first", "w4:p1")
        pid = fake_unit_host.live_pid(self)
        self.claude("second", "w4:p1", pid=pid, updated=2)
        with self.assertRaisesRegex(OSError, "trunk-unit: its mark is on") as caught:
            _ = unit_lookup.marked_units("show")
        for holder in (os.getpid(), pid):
            self.assertIn(f"pid {holder} (claude) in herdr:trunk:w4:p1", str(caught.exception))

    def test_the_command_prints_the_pane_and_tells_absent_from_cannot_tell(self) -> None:
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit")})
        self.claude("trunk", "w4:p1")
        found = self.run_script("pane", "show", "trunk-unit")
        self.assertEqual((found.returncode, found.stdout), (0, "herdr:trunk:w4:p1\n"))
        self.assertEqual(self.run_script("pane", "show", "fps-unit").returncode, 1)
        listed = self.run_script("list", "show")
        self.assertEqual(listed.stdout.split("\t")[:4], ["trunk-unit", "herdr:trunk:w4:p1", "trunk", "live"])
        _ = (self.root / "herdr-responses.json").write_text("not json")
        self.assertEqual(self.run_script("pane", "show", "trunk-unit").returncode, 3)
        self.assertEqual(self.run_script("list").returncode, 2)


    def test_a_herdr_unit_joins_its_newest_live_claude_and_deduplicates_processes(self) -> None:
        self.herdr_session()
        self.process(991001, 991000, ["zsh"], HERDR_PANE_ID="w1:p1",
                     SHOWRUNNER_UNIT="show", SHOWRUNNER_UNIT_ID="trunk-unit")
        self.claude("old", "", updated=1)
        self.claude("current", "", updated=2)
        found = unit_lookup.marked_units("show")
        self.assertEqual(list(found), ["trunk-unit"])
        unit = found["trunk-unit"]
        self.assertEqual((unit.place.session, unit.place), ("native", herdr.HerdrPlace("native", "w1:p1")))
        assert isinstance(unit.claude, LiveClaude)
        self.assertEqual(unit.claude.name, "current")
        self.assertEqual(self.run_script("pane", "show", "trunk-unit").stdout, "herdr:native:w1:p1\n")
        self.assertEqual(self.run_script("list", "show").stdout.split("\t")[:4],
                         ["trunk-unit", "herdr:native:w1:p1", "native", "live"])

    def test_attach_started_server_joins_live_claude_by_its_listening_socket(self) -> None:
        self.herdr_session(name="writing")
        server = self.root / "proc/991000"
        _ = (server / "cmdline").write_bytes(b"herdr\0server\0")
        _ = (server / "environ").write_bytes(b"HERDR_SESSION=writing\0")
        self.claude("writer", "", session_id="writer-id")
        found = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertEqual(found.place, herdr.HerdrPlace("writing", "w1:p1"))
        self.assertIsInstance(found.claude, LiveClaude)
        assert isinstance(found.claude, LiveClaude)
        self.assertEqual((found.claude.name, found.claude.session_id), ("writer", "writer-id"))
        self.assertEqual(self.run_script("list", "show").stdout.split("\t")[:4],
                         ["trunk-unit", "herdr:writing:w1:p1", "writing", "live"])

    def test_live_claude_with_deleted_cwd_joins_its_marked_shell(self) -> None:
        self.herdr_session(name="writing")
        self.process(991001, 991000, ["zsh"], HERDR_PANE_ID="w1:p1",
                     SHOWRUNNER_UNIT="show", SHOWRUNNER_UNIT_ID="trunk-unit")
        claude_cwd = self.root / "proc" / str(os.getpid()) / "cwd"
        claude_cwd.unlink()
        claude_cwd.symlink_to(self.root / "deleted-directory")
        self.claude("writer", "", session_id="writer-id")
        found = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertEqual(found.place, herdr.HerdrPlace("writing", "w1:p1"))
        self.assertIsInstance(found.claude, LiveClaude)
        assert isinstance(found.claude, LiveClaude)
        self.assertEqual((found.claude.pid, found.claude.session_id), (os.getpid(), "writer-id"))
        self.assertEqual(self.run_script("list", "show").stdout.split("\t")[:4],
                         ["trunk-unit", "herdr:writing:w1:p1", "writing", "live"])

    def test_a_herdr_unit_without_a_live_record_is_stopped_or_unknown(self) -> None:
        self.herdr_session()
        self.assertIsInstance(unit_lookup.marked_units("show")["trunk-unit"].claude, ClaudeNotRunning)
        _ = (self.records / "broken.json").write_text("{")
        self.assertIsInstance(unit_lookup.marked_units("show")["trunk-unit"].claude, ClaudeUnknown)


    def test_discovery_needs_only_herdr_when_path_has_no_session_tools(self) -> None:
        self.herdr_session()
        self.claude("current", "")
        run = subprocess.run
        with mock.patch("shutil.which", return_value=None), mock.patch.object(subprocess, "run", wraps=run) as commands:
            found = unit_lookup.every_marked_unit()
        self.assertEqual(list(found.units["show"]), ["trunk-unit"])
        self.assertTrue(commands.call_args_list)
        self.assertTrue(all(call.args[0][0] == self.environment["HERDR_BIN"] for call in commands.call_args_list))

    def test_unreadable_herdr_refuses_discovery_and_an_empty_answer_is_complete(self) -> None:
        with self.assertRaises(OSError):
            _ = unit_lookup.every_marked_unit()
        self.hosts({})
        self.assertEqual(unit_lookup.every_marked_unit(), unit_lookup.MarkedProductions({}, {}))

    def test_a_detached_run_carrying_the_marks_does_not_double_the_units_pane(self) -> None:
        # A worker build that mesh_runs supervises keeps the unit's marks and an old pane id.
        self.hosts({"$1": marked("agents", "w2:pS", "trunk-unit"), "$2": marked("runs", "w2:pE", "trunk-unit")})
        self.claude("trunk", "w2:pS")
        found = unit_lookup.every_marked_unit()
        self.assertEqual(found.faults, {})
        unit = found.units["show"]["trunk-unit"]
        self.assertEqual(unit.place, herdr.HerdrPlace("agents", "w2:pS"))
        self.assertIsInstance(unit.claude, LiveClaude)

    def test_leftover_marks_after_a_move_are_not_unit_sessions(self) -> None:
        self.hosts({"old": marked("old", "w3:p3Y", "old-unit", slug="former"),
                    "new": marked("new", "w4:p1", "new-unit")})
        self.claude("new", "w4:p1")
        for pid, argv in ((991030, ["python3", "-m", "unittest"]),
                          (991031, ["cargo", "test"]), (991032, ["codex", "app-server"])):
            self.process(pid, 991000, argv, HERDR_PANE_ID="w3:p3Y",
                         SHOWRUNNER_UNIT="former", SHOWRUNNER_UNIT_ID="old-unit")
        found = unit_lookup.every_marked_unit()
        self.assertEqual(found.faults, {})
        self.assertEqual({slug: list(units) for slug, units in found.units.items()},
                         {"former": ["old-unit"], "show": ["new-unit"]})
        self.assertIsInstance(found.units["former"]["old-unit"].claude, ClaudeNotRunning)
        self.assertIsInstance(found.units["show"]["new-unit"].claude, LiveClaude)

    def test_shell_marks_cannot_borrow_an_unmarked_claude_in_the_same_pane(self) -> None:
        self.hosts({"old": marked("old", "w4:p1", "old-unit")})
        self.claude("unmarked", "w4:p1")
        _ = (self.root / "proc" / str(os.getpid()) / "environ").write_bytes(b"HERDR_PANE_ID=w4:p1")
        found = unit_lookup.marked_units("show")["old-unit"]
        self.assertIsInstance(found.claude, ClaudeNotRunning)
        self.assertEqual(found.place, herdr.HerdrPlace("old", "w4:p1"))

    def test_exited_claude_leaves_its_shell_pane_available_for_recovery(self) -> None:
        self.hosts({"trunk": marked("trunk", "w4:p1", "trunk-unit")})
        self.claude("trunk", "w4:p1")
        self.assertIsInstance(unit_lookup.marked_units("show")["trunk-unit"].claude, LiveClaude)
        (self.records / "trunk-1.json").unlink()
        found = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertIsInstance(found.claude, ClaudeNotRunning)
        self.assertEqual(found.place, herdr.HerdrPlace("trunk", "w4:p1"))

    def test_live_claude_wins_over_leftover_marks_on_another_pane(self) -> None:
        self.hosts({"old": marked("old", "w3:p3Y", "trunk-unit"),
                    "new": marked("new", "w4:p1", "trunk-unit")})
        self.claude("new", "w4:p1")
        found = unit_lookup.every_marked_unit()
        self.assertEqual(found.faults, {})
        self.assertEqual(list(found.units["show"]), ["trunk-unit"])
        self.assertIsInstance(found.units["show"]["trunk-unit"].claude, LiveClaude)
        self.assertEqual(found.units["show"]["trunk-unit"].place, herdr.HerdrPlace("new", "w4:p1"))

    def test_a_running_registered_claude_without_a_socket_remains_unknown(self) -> None:
        self.hosts({"trunk": marked("trunk", "w4:p1", "trunk-unit")})
        self.claude("trunk", "w4:p1")
        record = self.records / "trunk-1.json"
        data = cast(dict[str, object], json.loads(record.read_text()))
        data["procStart"] = sessions.process_start(os.getpid())
        data["messagingSocketPath"] = ""
        _ = record.write_text(json.dumps(data))
        found = unit_lookup.marked_units("show")["trunk-unit"]
        self.assertIsInstance(found.claude, ClaudeUnknown)

    def test_unknown_duplicate_marks_still_refuse(self) -> None:
        self.hosts({"old": marked("old", "w3:p3Y", "trunk-unit"),
                    "new": marked("new", "w4:p1", "trunk-unit")})
        self.claude("new", "w4:p1")
        _ = (self.records / "broken.json").write_text("{")
        with self.assertRaisesRegex(OSError, "herdr:old:w3:p3Y"):
            _ = unit_lookup.marked_units("show")

    def test_one_duplicate_mark_leaves_out_only_its_own_unit(self) -> None:
        self.hosts({
            "first": marked("first", "w1:p1", "one-unit"),
            "copy": marked("copy", "w2:p1", "one-unit"),
            "third": marked("third", "w4:p1", "one-unit"),
            "two": marked("two", "w5:p1", "two-unit"),
            "other": marked("other", "w3:p1", "other-unit", slug="other"),
        })
        self.live_hosts("w1:p1", "w2:p1", "w4:p1", "w5:p1", "w3:p1")
        found = unit_lookup.every_marked_unit()
        self.assertEqual({slug: list(units) for slug, units in found.units.items()},
                         {"show": ["two-unit"], "other": ["other-unit"]})
        self.assertEqual(set(found.faults), {"show"})
        self.assertEqual(set(found.faults["show"]), {"one-unit"})
        for place in ("copy:w2:p1", "first:w1:p1", "third:w4:p1"):
            self.assertIn(f"herdr:{place}", found.faults["show"]["one-unit"])
        self.assertEqual(found.production_fault("other"), "")
        two = unit_lookup.marked_unit("show", "two-unit")
        self.assertEqual(two.place if two else None, herdr.HerdrPlace("two", "w5:p1"))
        self.assertIsNone(unit_lookup.marked_unit("show", "absent-unit"))
        with self.assertRaisesRegex(OSError, "one-unit: its mark is on"):
            _ = unit_lookup.marked_unit("show", "one-unit")
        with self.assertRaisesRegex(OSError, "one-unit: its mark is on"):
            _ = unit_lookup.marked_units("show")

    def record(self, name: str, worktree: Path, started: float, *later: str) -> Path:
        """Write the record of a /unit:direct run in `worktree`, as the recorder would."""
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        events = [{"event_type": "run_started", "working_dir": str(worktree.resolve()), "run_started_at": started},
                  *({"event_type": kind} for kind in later)]
        path = runs / f"{name}.jsonl"
        _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return path

    def test_a_units_run_state_is_read_from_the_newest_run_record_of_its_worktree(self) -> None:
        worktree = self.root / "trunk"
        worktree.mkdir()
        # No run was ever started here: the unit is standing by, whatever other worktrees ran.
        self.assertIs(unit_lookup.run_state(worktree), UnitState.STANDING_BY)
        _ = self.record("elsewhere", self.root / "elsewhere", 9.0, "phase_started")
        self.assertIs(unit_lookup.run_state(worktree), UnitState.STANDING_BY)
        _ = self.record("first", worktree, 1.0, "phase_started", "run_finished")
        self.assertIs(unit_lookup.run_state(worktree), UnitState.RUN_FINISHED)
        second = self.record("second", worktree, 2.0, "phase_started")
        self.assertIs(unit_lookup.run_state(worktree), UnitState.RUNNING)
        # A last line caught half-written is not yet an event.
        with second.open("a", encoding="utf-8") as handle:
            _ = handle.write('{"event_type": "run_fin')
        self.assertIs(unit_lookup.run_state(worktree), UnitState.RUNNING)
        _ = self.record("second", worktree, 2.0, "phase_started", "run_finished")
        self.assertIs(unit_lookup.run_state(worktree), UnitState.RUN_FINISHED)

    def test_run_records_that_cannot_be_read_are_an_error_not_a_state(self) -> None:
        (self.root / "history/runs/unreadable.jsonl").mkdir(parents=True)
        with self.assertRaises(OSError):
            _ = unit_lookup.run_state(self.root / "trunk")

    def test_only_the_first_and_last_events_are_parsed(self) -> None:
        tree = self.root / "trunk"
        path = self.record("run", tree, 1.0)
        first = path.read_text(encoding="utf-8").strip()
        last = json.dumps({"event_type": "run_finished"})
        _ = path.write_text(first + "\nnot JSON\n[also broken\n" + last + "\n", encoding="utf-8")
        with mock.patch.object(json, "loads", wraps=json.loads) as parses:
            self.assertIs(unit_lookup.run_state(tree), UnitState.RUN_FINISHED)
        self.assertEqual(parses.call_args_list, [mock.call(first + "\n"), mock.call(last)])

    def test_last_event_larger_than_a_read_block(self) -> None:
        tree = self.root / "trunk"
        path = self.record("run", tree, 1.0)
        with path.open("a", encoding="utf-8") as handle:
            _ = handle.write(json.dumps({"event_type": "run_finished", "detail": "界" * (64 * 1024)},
                                        ensure_ascii=False) + "\n")
        self.assertIs(unit_lookup.run_state(tree), UnitState.RUN_FINISHED)

    def test_partial_last_line_uses_the_previous_complete_object(self) -> None:
        tree = self.root / "trunk"
        path = self.record("run", tree, 1.0, "run_finished")
        with path.open("a", encoding="utf-8") as handle:
            _ = handle.write('[]\nnull\n{"event_type": "phase_star')
        self.assertIs(unit_lookup.run_state(tree), UnitState.RUN_FINISHED)

    def test_empty_partial_and_non_start_first_lines_are_skipped(self) -> None:
        runs = self.root / "history/runs"
        runs.mkdir(parents=True)
        for name, first in (("empty", ""), ("partial", '{"event_type": "run_star'),
                            ("object", '{}\n'), ("array", '[]\n')):
            with self.subTest(name=name):
                _ = (runs / f"{name}.jsonl").write_text(first, encoding="utf-8")
        # A later valid run_started cannot rescue a partial or unrelated first line.
        for path in runs.iterdir():
            if path.name == "empty.jsonl":
                continue
            with path.open("a", encoding="utf-8") as handle:
                _ = handle.write('\n' + json.dumps({"event_type": "run_started",
                    "working_dir": str(self.root / "trunk"), "run_started_at": 10}) + '\n')
        self.assertEqual(unit_lookup.read_run_states().records, {})

    def test_one_snapshot_answers_for_several_worktrees_without_rereading(self) -> None:
        first, second = self.root / "first", self.root / "second"
        old = self.record("old", first, 1.0, "run_finished")
        newest = self.record("new", first, 2.0, "phase_started")
        finished = self.record("second", second, 3.0, "run_finished")
        with mock.patch.object(unit_lookup, "runs_dir", wraps=unit_lookup.runs_dir) as reads:
            runs = unit_lookup.read_run_states()
            # An already-built value keeps the tick's view, even when records change.
            old.unlink()
            newest.unlink()
            finished.unlink()
            self.assertIs(runs.state(first), UnitState.RUNNING)
            self.assertIs(runs.state(second), UnitState.RUN_FINISHED)
            self.assertIs(runs.state(self.root / "absent"), UnitState.STANDING_BY)
            self.assertIs(runs.state(first / ".." / "first"), UnitState.RUNNING)
            self.assertEqual(runs.records[str(first)], unit_lookup.RunRecord(newest, UnitState.RUNNING))
            reads.assert_called_once_with()

    def test_bad_utf8_in_first_or_last_line_is_an_error(self) -> None:
        for where in ("first", "last"):
            with self.subTest(where=where):
                path = self.record("run", self.root / "trunk", 1.0)
                if where == "first":
                    _ = path.write_bytes(b'\xff\n')
                else:
                    with path.open("ab") as handle:
                        _ = handle.write(b'\xff\n')
                with self.assertRaises(UnicodeError):
                    _ = unit_lookup.read_run_states()


if __name__ == "__main__":
    _ = unittest.main()
