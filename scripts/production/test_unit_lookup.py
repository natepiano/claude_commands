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
from typing import override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_unit_host
import showrunners
import unit_lookup
import herdr
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
                            "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "history"),
                            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
                            "HERDR_PROC": str(self.root / "proc"),
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

    def claude(self, name: str, pane: str, *, updated: int = 1, session_id: str = "") -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        if pane:
            fake_unit_host.place_process(self.environment, os.getpid(), pane)
        path = self.root / f"{name}-{updated}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.records / f"{name}-{updated}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": session_id or f"id-{name}", "name": name,
            "messagingSocketPath": str(path), "updatedAt": updated, "tmux": f"label-at-start:@1.{pane}" if pane else ""}))

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


    def test_former_and_current_marks_cannot_hide_duplicate_unit_sessions(self) -> None:
        self.renamed_production()
        self.hosts({"$1": marked("before", "w4:p1", "trunk-unit", slug="old"),
                                     "$2": marked("after", "w5:p1", "trunk-unit", slug="new")})
        found = unit_lookup.every_marked_unit()
        self.assertEqual(found.units, {})
        self.assertIn("both carry the mark of trunk-unit", found.faults["new"])
        for slug in ("old", "new"):
            with self.subTest(slug=slug):
                with self.assertRaisesRegex(OSError, "both carry the mark of trunk-unit"):
                    _ = unit_lookup.marked_units(slug)
                for arguments in (("list", slug), ("pane", slug, "trunk-unit")):
                    result = self.run_script(*arguments)
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("both carry the mark of trunk-unit", result.stderr)

    def test_sessions_sharing_a_production_read_its_timer_once_per_discovery(self) -> None:
        self.renamed_production()
        self.hosts({
            "$1": marked("first", "w4:p1", "first-unit", slug="old"),
            "$2": marked("second", "w5:p1", "second-unit", slug="old"),
            "$3": marked("third", "w6:p1", "third-unit", slug="old"),
        })
        with mock.patch.object(showrunners, "checked_doc", wraps=showrunners.checked_doc) as reads:
            for expected_reads in (1, 2):
                found = unit_lookup.every_marked_unit()
                self.assertEqual(set(found.units["new"]), {"first-unit", "second-unit", "third-unit"})
                self.assertEqual(reads.call_count, expected_reads)

    def test_current_production_discovery_keeps_an_existing_timer_read_budget(self) -> None:
        self.renamed_production()
        self.hosts({"$1": marked("current", "w4:p1", "trunk-unit", slug="new")})
        with mock.patch.object(showrunners, "checked_doc", wraps=showrunners.checked_doc) as reads:
            checked = showrunners.checked_doc(self.root / "notifier/showrunner-new")
            self.assertIsInstance(checked, showrunners.CheckedDoc)
            found = unit_lookup.every_marked_unit()
            self.assertEqual(list(found.units["new"]), ["trunk-unit"])
            self.assertEqual(reads.call_count, 1, "current marks must not reread already inventoried timers")

    def test_current_production_lookup_does_not_read_timer_docs(self) -> None:
        self.renamed_production()
        self.hosts({"$1": marked("current", "w4:p1", "trunk-unit", slug="new")})
        with mock.patch.object(showrunners, "checked_doc", wraps=showrunners.checked_doc) as reads:
            found = unit_lookup.marked_units("new")
            self.assertEqual(found["trunk-unit"].place, herdr.HerdrPlace("current", "w4:p1"))
            self.assertEqual(reads.call_count, 0)

    def test_herdr_marks_are_grouped_under_the_current_production(self) -> None:
        self.renamed_production()
        self.herdr_session()
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


    def test_two_sessions_carrying_one_units_mark_are_an_error(self) -> None:
        self.hosts({"$1": marked("trunk", "w4:p1", "trunk-unit"), "$2": marked("copy", "w5:p1", "trunk-unit")})
        with self.assertRaisesRegex(OSError, "both carry the mark of trunk-unit"):
            _ = unit_lookup.marked_units("show")


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

    def test_one_duplicate_production_does_not_hide_other_productions(self) -> None:
        self.hosts({
            "first": marked("first", "w1:p1", "one-unit"),
            "copy": marked("copy", "w2:p1", "one-unit"),
            "other": marked("other", "w3:p1", "other-unit", slug="other"),
        })
        found = unit_lookup.every_marked_unit()
        self.assertEqual(list(found.units), ["other"])
        self.assertIn("herdr sessions first and copy both carry the mark of one-unit", found.faults["show"])

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


if __name__ == "__main__":
    _ = unittest.main()
