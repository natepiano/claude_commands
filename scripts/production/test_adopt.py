"""Tests for moving a production from written-down unit session names to the lookup."""
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

import fake_tmux
from fake_tmux import FakeSession

SCRIPT = Path(__file__).with_name("adopt.py")


class AdoptTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    config: Path = Path()
    tmux: Path = Path()
    scratch: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout = self.root / "checkout"
        self.doc = self.checkout / "docs/plans/show-production.md"
        self.config = self.root / "showrunners.json"
        self.tmux = self.root / "tmux.json"
        self.scratch = self.root / "scratch"
        for directory in (self.doc.parent, self.root / "sessions", self.root / "home", self.scratch / "unit_status"):
            directory.mkdir(parents=True)
        self.environment = {**os.environ, "HOME": str(self.root / "home"), "GIT_CONFIG_GLOBAL": os.devnull,
                            "SHOWRUNNERS_CONFIG": str(self.config), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                            "UNIT_LOOKUP_TMUX": str(Path(__file__).with_name("fake_tmux.py")),
                            "FAKE_TMUX_STATE": str(self.tmux)}
        # alpha's session keeps the name written down; beta's was renamed in Claude and its label followed.
        for name in ("alpha", "beta"):
            (self.root / name).mkdir()
            _ = (self.root / name / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        _ = self.doc.write_text("\n".join((
            "# Production — show", "", "## Production Context", "", "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Showrunner session:** director",
            "- **Log:** `production.log`", "- **User zone:** America/Los_Angeles", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| `alpha-unit` | `docs/alpha.md` | `{self.root / 'alpha'}` | `alpha` | `alpha` | — | `a.txt` |",
            f"| `beta-unit` | `docs/beta.md` | `{self.root / 'beta'}` | `beta` | `beta` | 4100 | — |",
            f"| `done-unit` | `docs/done.md` | `{self.root / 'no-worktree'}` | `done` | `done` | — | — |",
            "", "## Gates", "")) + "\n", encoding="utf-8")
        for arguments in (["init", "-b", "production"], ["config", "user.name", "Adopt Test"],
                          ["config", "user.email", "adopt@example.invalid"], ["add", "."], ["commit", "-m", "base"]):
            _ = subprocess.run(["git", "-C", str(self.checkout), *arguments], env=self.environment, check=True,
                               capture_output=True)
        _ = self.config.write_text(json.dumps({
            "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "natedev", "always": [],
            "showrunners": [{"session": "director", "zone": "America/Los_Angeles",
                             "units": [{"session": "alpha", "status": "running"},
                                       {"session": "beta", "status": "run-finished"}]}]}), encoding="utf-8")
        fake_tmux.write(self.tmux, {"$1": FakeSession(label="alpha", panes=["%1"], env={}),
                                    "$2": FakeSession(label="beta-renamed", panes=["%2"], env={}),
                                    "$3": FakeSession(label="someone-else", panes=["%3"], env={})})
        self.claude("beta-renamed", "%2", former=["beta"])
        _ = (self.scratch / "unit_status/decisions_seen").write_text("alpha|which colour\nbeta|which size\n",
                                                                    encoding="utf-8")

    def claude(self, name: str, pane: str, *, former: list[str]) -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        path = self.root / f"{name}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.root / "sessions" / f"{name}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": f"id-{name}", "name": name, "formerNames": former,
            "messagingSocketPath": str(path), "updatedAt": 1, "tmux": f"label-at-start:@1.{pane}"}))

    def run_adopt(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), str(self.doc), "--scratch", str(self.scratch), *extra],
                              env=self.environment, capture_output=True, text=True, check=False)

    def marks(self) -> dict[str, dict[str, str]]:
        return {session["label"]: session["env"] for session in fake_tmux.read(self.tmux).values()}

    def commits(self) -> list[str]:
        return subprocess.run(["git", "-C", str(self.checkout), "log", "--format=%s"], env=self.environment,
                              capture_output=True, text=True, check=True).stdout.splitlines()

    def everything(self) -> tuple[bytes, bytes, bytes, bytes]:
        return (self.doc.read_bytes(), self.tmux.read_bytes(), self.config.read_bytes(),
                (self.scratch / "unit_status/decisions_seen").read_bytes())

    def test_each_unit_is_marked_and_no_copy_of_its_name_is_left(self) -> None:
        registry = self.config.read_bytes()
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.marks(), {
            # The old registry held a run state per unit; none is carried over, as none is stored.
            "alpha": {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "alpha-unit"},
            "beta-renamed": {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "beta-unit"},
            "someone-else": {}})
        table = [line for line in self.doc.read_text(encoding="utf-8").splitlines() if line.startswith("|")]
        self.assertEqual(table[:2], ["| Unit | Plan | Worktree | Branch | Port | Owns |",
                                     "| --- | --- | --- | --- | --- | --- |"])
        self.assertEqual(table[3], f"| `beta-unit` | `docs/beta.md` | `{self.root / 'beta'}` | `beta` | 4100 | — |")
        # The registry is read for the old run states and never written.
        self.assertEqual(self.config.read_bytes(), registry)
        self.assertEqual((self.scratch / "unit_status/decisions_seen").read_text(encoding="utf-8"),
                         "alpha-unit|which colour\nbeta-unit|which size\n")
        self.assertEqual(self.commits(), ["production(show): unit sessions are looked up, not written down", "base"])

    def test_a_unit_marked_by_hand_before_the_run_is_left_as_it_is(self) -> None:
        sessions = fake_tmux.read(self.tmux)
        sessions["$2"]["env"].update({"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "beta-unit"})
        fake_tmux.write(self.tmux, sessions)
        result = self.run_adopt()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("adopt: beta-unit was already marked", result.stdout)
        self.assertEqual(self.marks()["beta-renamed"], {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "beta-unit"})
        self.assertEqual(self.marks()["alpha"], {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "alpha-unit"})

    def test_a_second_run_changes_nothing(self) -> None:
        self.assertEqual(self.run_adopt().returncode, 0)
        before, commits = self.everything(), self.commits()
        again = self.run_adopt()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("nothing to do", again.stdout)
        self.assertEqual((self.everything(), self.commits()), (before, commits))

    def test_a_unit_whose_session_is_not_found_leaves_everything_as_it_was(self) -> None:
        sessions = fake_tmux.read(self.tmux)
        del sessions["$1"]
        fake_tmux.write(self.tmux, sessions)
        before, commits = self.everything(), self.commits()
        result = self.run_adopt()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("alpha-unit (written down as alpha)", result.stdout)
        self.assertIn("unit_lookup.py mark show <unit> <tmux session>", result.stdout)
        self.assertEqual((self.everything(), self.commits()), (before, commits))

    def test_a_name_two_sessions_answer_to_is_not_found(self) -> None:
        self.claude("another", "%3", former=["alpha"])
        before = self.everything()
        result = self.run_adopt()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.everything(), before)

    def test_the_showrunner_can_say_a_unit_has_no_session_now(self) -> None:
        sessions = fake_tmux.read(self.tmux)
        del sessions["$1"]
        fake_tmux.write(self.tmux, sessions)
        result = self.run_adopt("--gone", "alpha-unit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("alpha-unit has no session now", result.stdout)
        self.assertEqual(self.marks()["beta-renamed"]["SHOWRUNNER_UNIT_ID"], "beta-unit")
        self.assertNotIn("Session", self.doc.read_text(encoding="utf-8").split("## Units")[1].split("## Gates")[0])
        self.assertIn("alpha-unit|which colour", (self.scratch / "unit_status/decisions_seen").read_text())

    def test_a_tmux_that_cannot_say_stops_it_before_anything_changes(self) -> None:
        self.environment["UNIT_LOOKUP_TMUX"] = str(self.root / "no-such-tmux")
        before = self.everything()
        result = self.run_adopt()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.everything(), before)


if __name__ == "__main__":
    _ = unittest.main()
