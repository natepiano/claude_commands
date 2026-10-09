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
from typing import override

import fake_showrunner
import fake_tmux
from fake_tmux import FakeSession

SCRIPT = Path(__file__).with_name("live_units.py")
FAKE = str(Path(__file__).with_name("fake_tmux.py"))
OLD_HEADER = ("| Unit | Plan | Worktree | Branch | Session | Port | Owns |", "| --- | --- | --- | --- | --- | --- | --- |")
HEADER = ("| Unit | Plan | Worktree | Branch | Port | Owns |", "| --- | --- | --- | --- | --- | --- |")


def marked(label: str, pane: str, unit: str, slug: str = "show") -> FakeSession:
    return FakeSession(label=label, panes=[pane], env={"SHOWRUNNER_UNIT": slug, "SHOWRUNNER_UNIT_ID": unit})


class LiveUnitsTests(unittest.TestCase):
    root: Path = Path()
    timers: Path = Path()
    doc: Path = Path()
    state: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.timers = self.root / "notifier"
        self.doc = self.root / "show-production.md"
        self.state = self.root / "tmux.json"
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.environment = {**os.environ, "NOTIFIER_STATE_DIR": str(self.timers),
                            "NOTIFIER_SESSIONS_DIR": str(self.records),
                            "UNIT_LOOKUP_TMUX": FAKE, "FAKE_TMUX_STATE": str(self.state)}

    def write_doc(self, header: tuple[str, str], *rows: str) -> None:
        _ = self.doc.write_text("\n".join(("# Production", "## Units", *header, *rows)), encoding="utf-8")

    def claude(self, name: str, pane: str) -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        path = self.root / f"{name}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.records / f"{name}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": f"id-{name}", "name": name,
            "messagingSocketPath": str(path), "updatedAt": 1, "tmux": f"label-at-start:@1.{pane}"}))

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
        fake_tmux.write(self.state, {"$1": marked("beta-label", "%5", "beta-unit"),
                                     "$2": marked("renamed-since-launch", "%4", "alpha-unit"),
                                     "$3": marked("done-label", "%6", "done-unit"),
                                     "$4": marked("other-show", "%7", "gone-unit", slug="other")})
        self.claude("renamed-since-launch", "%4")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        # `gone-unit` has neither a worktree nor a session of this production, so it is retired.
        self.assertEqual(result.stdout.splitlines(), ["alpha-unit\t%4\tlive\trenamed-since-launch",
                                                      "beta-unit\t%5\tstopped\t"])

    def test_a_doc_that_still_has_the_session_column_lists_the_same_units(self) -> None:
        self.write_doc(OLD_HEADER,
                       "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | `stale-name` | — | — |")
        fake_tmux.write(self.state, {"$1": marked("current-name", "%4", "alpha-unit")})
        self.claude("current-name", "%4")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["alpha-unit\t%4\tlive\tcurrent-name"])

    def test_a_prompt_written_before_the_change_still_names_the_showrunner(self) -> None:
        self.write_doc(HEADER, "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | — | — |")
        fake_tmux.write(self.state, {"$1": marked("alpha-label", "%4", "alpha-unit")})
        _ = fake_showrunner.write_timer(self.timers, "show", "director-id", "America/Los_Angeles", self.doc)
        self.addCleanup(fake_showrunner.write_session(self.records, "director", "director-id").close)
        for showrunner in ("director", "show"):
            with self.subTest(showrunner=showrunner):
                result = self.run_script(showrunner)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["alpha-unit\t%4\tstopped\t"])

    def test_a_tmux_that_cannot_say_is_an_error_not_an_empty_list(self) -> None:
        self.write_doc(HEADER, "| alpha-unit | docs/plans/alpha.md | /tmp/no-such-worktree-alpha | alpha | — | — |")
        self.environment["UNIT_LOOKUP_TMUX"] = str(self.root / "no-such-tmux")
        result = self.run_script()
        self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
        self.assertIn("no-such-tmux", result.stderr)

    def test_a_showrunner_no_timer_names_exits_one(self) -> None:
        result = self.run_script("missing")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("no showrunner with an update timer is called missing", result.stderr)
        self.assertEqual(self.run_script().returncode, 1)
        self.assertEqual(self.run_script("--production").returncode, 2)


if __name__ == "__main__":
    _ = unittest.main()
