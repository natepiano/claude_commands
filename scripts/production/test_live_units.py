"""Tests for listing a showrunner's units from its production doc and the marks on their sessions."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.lib.process_start_test_support import write_stat
from typing import override

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))

from scripts.lib.socket_test_support import short_socket_directory
from fake_herdr import herdr_listener
import fake_showrunner
import fake_unit_host
from fake_unit_host import HostedUnit

SCRIPT = Path(__file__).with_name("live_units.py")
OLD_HEADER = ("| Unit | Plan | Worktree | Branch | Session | Port | Owns |", "| --- | --- | --- | --- | --- | --- | --- |")
HEADER = ("| Unit | Plan | Worktree | Branch | Port | Owns |", "| --- | --- | --- | --- | --- | --- |")


def marked(label: str, pane: str, unit: str, slug: str = "show") -> HostedUnit:
    return HostedUnit(label, pane, {"SHOWRUNNER_UNIT": slug, "SHOWRUNNER_UNIT_ID": unit})


class LiveUnitsTests(unittest.TestCase):
    root: Path = Path()
    timers: Path = Path()
    doc: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.timers = self.root / "notifier"
        self.doc = self.root / "show-production.md"
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.environment = {**os.environ, "NOTIFIER_STATE_DIR": str(self.timers),
                            "NOTIFIER_SESSIONS_DIR": str(self.records),
                            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
                            "HERDR_PROC": str(self.root / "proc"),
                            "HERDR_PLATFORM": "linux",
                            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
                            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json")}
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"stdout": "", "code": 1}}))

    def hosts(self, units: dict[str, HostedUnit]) -> None:
        fake_unit_host.write(self.environment, list(units.values()))

    def write_doc(self, header: tuple[str, str], *rows: str) -> None:
        _ = self.doc.write_text("\n".join(("# Production", "## Units", *header, *rows)), encoding="utf-8")

    def claude(self, name: str, pane: str) -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        if pane:
            fake_unit_host.place_process(self.environment, os.getpid(), pane)
        path = Path(self.enterContext(short_socket_directory())) / f"{name}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.records / f"{name}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": f"id-{name}", "name": name,
            "messagingSocketPath": str(path), "updatedAt": 1, "tmux": f"label-at-start:@1.{pane}" if pane else ""}))

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *(arguments or ("--production", str(self.doc)))],
                              env=self.environment, capture_output=True, text=True, check=False)

    def test_each_live_row_is_listed_in_doc_order_with_its_session_as_it_is_now(self) -> None:
        self.write_doc(
            HEADER,
            "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | — | — |",
            "| done-unit | (retired by the user; run done) follow-up | /tmp/no-such-worktree-done | done | — | — |",
            "| beta-unit | docs/plans/beta.md | /tmp/no-such-worktree-beta | beta | — | — |",
            "| gone-unit | docs/plans/gone.md | /tmp/no-such-worktree-gone | gone | — | — |",
        )
        self.hosts({"$1": marked("beta-label", "w5:p1", "beta-unit"),
                                     "$2": marked("renamed-since-launch", "w4:p1", "alpha-unit"),
                                     "$3": marked("done-label", "w6:p1", "done-unit"),
                                     "$4": marked("other-show", "w7:p1", "gone-unit", slug="other")})
        self.claude("renamed-since-launch", "w4:p1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        # `gone-unit` has neither a worktree nor a session of this production, so it is retired.
        self.assertEqual(result.stdout.splitlines(), ["alpha-unit\therdr:renamed-since-launch:w4:p1\tlive\trenamed-since-launch",
                                                      "beta-unit\therdr:beta-label:w5:p1\tstopped\t"])

    def test_herdr_pane_targets_match_between_live_units_and_lookup(self) -> None:
        self.write_doc(HEADER, "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | — | — |")
        self.hosts({"$1": marked("alpha-label", "w41:p1", "alpha-unit")})
        listed = self.run_script()
        target = subprocess.run([sys.executable, str(SCRIPT.with_name("unit_lookup.py")),
                                 "pane", "show", "alpha-unit"], env=self.environment,
                                capture_output=True, text=True, check=False)
        self.assertEqual((listed.returncode, target.returncode), (0, 0), listed.stderr + target.stderr)
        self.assertEqual(listed.stdout.split("\t")[1], "herdr:alpha-label:w41:p1")
        self.assertEqual(target.stdout, "herdr:alpha-label:w41:p1\n")

    def test_a_herdr_unit_is_listed_with_its_live_claude(self) -> None:
        self.write_doc(HEADER, "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | — | — |")
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"code": 0, "stdout": json.dumps({
            "sessions": [{"name": "native", "running": True, "default": False,
                          "session_dir": "/fixture", "socket_path": "/fixture/socket"}]
        })}}))
        for pid, parent, argv, environment in (
            (991000, 0, ["herdr", "--session", "native", "server"], {}),
            (os.getpid(), 991000, ["claude"], {"HERDR_PANE_ID": "w1:p1", "SHOWRUNNER_UNIT": "show",
                                             "SHOWRUNNER_UNIT_ID": "alpha-unit"}),
        ):
            directory = self.root / "proc" / str(pid)
            directory.mkdir(parents=True)
            _ = write_stat(directory.parent, int(directory.name), ppid=parent)
            _ = (directory / "cmdline").write_bytes("\0".join(argv).encode())
            _ = (directory / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in environment.items()).encode())
            (directory / "cwd").symlink_to(self.root)
        herdr_listener(self.root / "proc", 991000, "native", self.environment)
        self.claude("native-claude", "")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "alpha-unit\therdr:native:w1:p1\tlive\tnative-claude\n")

    def test_a_doc_that_still_has_the_session_column_lists_the_same_units(self) -> None:
        self.write_doc(OLD_HEADER,
                       "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | `stale-name` | — | — |")
        self.hosts({"$1": marked("current-name", "w4:p1", "alpha-unit")})
        self.claude("current-name", "w4:p1")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["alpha-unit\therdr:current-name:w4:p1\tlive\tcurrent-name"])

    def test_a_prompt_written_before_the_change_still_names_the_showrunner(self) -> None:
        self.write_doc(HEADER, "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | — | — |")
        self.hosts({"$1": marked("alpha-label", "w4:p1", "alpha-unit")})
        _ = fake_showrunner.write_timer(self.timers, "show", "director-id", "America/Los_Angeles", self.doc)
        self.addCleanup(fake_showrunner.write_session(self.records, "director", "director-id").close)
        for showrunner in ("director", "show"):
            with self.subTest(showrunner=showrunner):
                result = self.run_script(showrunner)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["alpha-unit\therdr:alpha-label:w4:p1\tstopped\t"])

    def test_a_host_that_cannot_say_is_an_error_not_an_empty_list(self) -> None:
        self.write_doc(HEADER, "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | — | — |")
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
