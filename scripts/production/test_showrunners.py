"""Showrunners read from their update timers, their names looked up at each call."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

import fake_tmux
import showrunners
from fake_tmux import FakeSession


SCRIPT = Path(__file__).with_name("showrunners.py")
FAKE = str(Path(__file__).with_name("fake_tmux.py"))
LOS_ANGELES = "America/Los_Angeles"


class ShowrunnerRegistryTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config: Path = Path()
        self.notifier: Path = Path()
        self.sessions: Path = Path()
        self.sessions_script: Path = Path()
        self.tmux: Path = Path()
        self.doc: Path = Path()
        self.children: list[subprocess.Popen[bytes]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "config" / "showrunners.json"
        self.config.parent.mkdir()
        self.notifier = self.root / "notifier"
        self.notifier.mkdir()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.sessions_script = self.root / "sessions.py"
        self.tmux = self.root / "tmux.json"
        self.doc = self.root / "show-production.md"
        self.addCleanup(self.close_processes)
        _ = self.sessions_script.write_text("""import json, os, pathlib, sys
records = [json.loads(path.read_text()) for path in pathlib.Path(os.environ['NOTIFIER_SESSIONS_DIR']).glob('*.json')]
command, target = sys.argv[1:]
for record in records:
    if not record['running']:
        continue
    if command == 'socket' and target in (record['name'], 'session:' + record['sessionId']):
        print(record['messagingSocketPath'])
        raise SystemExit(0)
    if command == 'id' and target == str(record['pid']):
        print(record['sessionId'])
        raise SystemExit(0)
raise SystemExit(1)
""")

    def close_processes(self) -> None:
        for child in self.children:
            child.terminate()
            _ = child.wait(timeout=2)

    def record(self, name: str, session_id: str, *, running: bool = True, pid: int | None = None) -> int:
        if pid is None:
            child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.children.append(child)
            pid = child.pid
        _ = (self.sessions / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "name": name, "sessionId": session_id,
            "messagingSocketPath": str(self.root / f"{session_id}.sock"),
            "updatedAt": 1, "running": running,
        }))
        return pid

    def instance(self, name: str, *, target: str = "session:live-id", zone: str = "America/Los_Angeles",
                 units: tuple[str, ...] = ("hook", "tool-based-ui-geometry-material"),
                 include_target: bool = True, include_prompt: bool = True, doc: Path | None = None) -> None:
        directory = self.notifier / name
        directory.mkdir()
        prompt = directory / "prompt"
        _ = prompt.write_text("Run `zsh ~/.claude/scripts/production/unit_status.sh /tmp/run/unit_status "
                              + f"{zone} {' '.join(units)} | cut -c1-400`.\n")
        fields = ["EVERY=15"]
        if include_target:
            fields.append(f"TARGET={target}")
        if include_prompt:
            fields.append(f"PROMPT_FILE={prompt}")
        if doc is not None:
            fields.append("CHECK=" + shlex.join(["zsh", "/opt/tools/production_check.sh", str(doc)]))
        _ = (directory / "conf").write_text("\n".join(fields) + "\n")

    def environment(self, pane: str = "") -> dict[str, str]:
        # `TMUX_PANE` is set here always: a test run from inside tmux must not hand on its own pane.
        return {**os.environ, "SHOWRUNNERS_CONFIG": str(self.config),
                "NOTIFIER_STATE_DIR": str(self.notifier), "NOTIFIER_SESSIONS_DIR": str(self.sessions),
                "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
                "UNIT_LOOKUP_TMUX": FAKE, "FAKE_TMUX_STATE": str(self.tmux), "TMUX_PANE": pane}

    def cli(self, *args: str, pane: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.environment(pane),
                              capture_output=True, text=True, check=False)

    def successful(self, *args: str, pane: str = "") -> str:
        result = self.cli(*args, pane=pane)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout

    def unit_session(self, unit: str, pane: str, state: str = "") -> None:
        """Give the production `show` one tmux session marked as `unit`."""
        marks = {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": unit}
        if state:
            marks["SHOWRUNNER_UNIT_STATE"] = state
        sessions = fake_tmux.read(self.tmux) if self.tmux.exists() else {}
        sessions[f"${len(sessions) + 1}"] = FakeSession(label=f"label-of-{unit}", panes=[pane], env=marks)
        fake_tmux.write(self.tmux, sessions)

    def state_marks(self) -> dict[str, str]:
        return {session["env"]["SHOWRUNNER_UNIT_ID"]: session["env"].get("SHOWRUNNER_UNIT_STATE", "")
                for session in fake_tmux.read(self.tmux).values() if "SHOWRUNNER_UNIT_ID" in session["env"]}

    def test_checked_doc_reads_absolute_path_after_check_script(self) -> None:
        directory = self.notifier / "showrunner-check"
        directory.mkdir()
        doc = self.root / "example-production.md"
        check = shlex.join(["zsh", "/opt/tools/production_check.sh", str(doc), "extra"])
        _ = (directory / "conf").write_text(f"TARGET=session:abc\nCHECK={check}\n", encoding="utf-8")
        self.assertEqual(showrunners.checked_doc(directory), showrunners.CheckedDoc(doc))

    def test_checked_doc_refuses_missing_check_line(self) -> None:
        directory = self.notifier / "showrunner-check"
        directory.mkdir()
        _ = (directory / "conf").write_text("TARGET=session:abc\n", encoding="utf-8")
        self.assertIsInstance(showrunners.checked_doc(directory), showrunners.NoCheckedDoc)

    def test_checked_doc_refuses_relative_production_path(self) -> None:
        directory = self.notifier / "showrunner-check"
        directory.mkdir()
        _ = (directory / "conf").write_text(
            "CHECK=zsh /opt/tools/production_check.sh docs/example-production.md\n", encoding="utf-8")
        result = showrunners.checked_doc(directory)
        self.assertEqual(result, showrunners.NoCheckedDoc("production doc path is relative"))

    def test_list_shows_each_showrunner_under_the_name_it_has_now(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        self.instance("showrunner-other", target="session:stopped-id")
        self.instance("stall-watch", doc=self.doc)
        pid = self.record("first-name", "live-id")
        self.assertEqual(self.successful("list").splitlines(), [
            f"other\t<not running>\t{LOS_ANGELES}\t<no doc in its check>",
            f"show\tfirst-name\t{LOS_ANGELES}\t{self.doc}"])
        self.assertEqual(self.successful("name", "show"), "first-name\n")
        # A rename is written in one place, the session's own record, and every reader sees it.
        _ = self.record("renamed", "live-id", pid=pid)
        self.assertIn("show\trenamed\t", self.successful("list"))
        self.assertEqual(self.successful("name", "show"), "renamed\n")
        self.assertFalse(self.config.exists())

    def test_name_refuses_a_showrunner_that_is_not_running_or_has_no_timer(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        for slug in ("show", "absent"):
            with self.subTest(slug=slug):
                result = self.cli("name", slug)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn(f"the showrunner of {slug} is not running", result.stderr)

    def test_unreadable_session_records_are_an_error_not_a_stopped_showrunner(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        self.assertIn("show\t<not running>", self.successful("list"))
        _ = self.sessions_script.write_text(
            "import sys\nprint('sessions: one or more registry files could not be read', file=sys.stderr)\n"
            + "raise SystemExit(3)\n")
        result = self.cli("list")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("not running", result.stdout)
        self.assertIn("cannot tell whether session:live-id is running: sessions: one or more registry files",
                      result.stderr)

    def test_a_timer_that_is_not_a_complete_showrunners_is_left_out_with_the_reason(self) -> None:
        self.instance("showrunner-no-target", include_target=False)
        self.instance("showrunner-no-prompt", include_prompt=False)
        self.instance("showrunner-unnamed", target="session:unnamed-id")
        self.instance("showrunner-show", doc=self.doc)
        _ = self.record("director", "live-id")
        # A session that answers on its socket and has no live named process cannot be named.
        _ = self.record("gone", "unnamed-id", pid=99999999)
        result = self.cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"show\tdirector\t{LOS_ANGELES}\t{self.doc}"])
        self.assertIn("skipping showrunner-no-target", result.stderr)
        self.assertIn("skipping showrunner-no-prompt: PROMPT_FILE is missing", result.stderr)
        self.assertIn("skipping showrunner-unnamed: session unnamed-id has no live named process", result.stderr)

    def test_the_name_comes_from_the_live_process_not_a_stale_record_of_the_same_session(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        _ = self.record("stale-name", "live-id", pid=99999999)
        _ = self.record("director", "live-id")
        self.assertEqual(self.successful("name", "show"), "director\n")

    def test_ready_marks_a_standing_by_unit_running_by_the_showrunners_name_or_its_production(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        _ = self.record("director", "live-id")
        self.unit_session("alpha", "%4", "standing-by")
        self.unit_session("beta", "%5", "standing-by")
        self.unit_session("gamma", "%6", "standing-by")
        _ = self.successful("ready", "director", "--unit", "alpha")
        _ = self.successful("ready", "show", "--unit", "beta")
        self.assertEqual(self.state_marks(), {"alpha": "running", "beta": "running", "gamma": "standing-by"})
        self.assertFalse(self.config.exists())

    def test_ready_non_standby_unit_says_so_without_changing_its_mark(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        self.unit_session("alpha", "%4", "run-finished")
        result = self.cli("ready", "show", "--unit", "alpha", "--unit", "no-session")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout.splitlines(), ["alpha is not on standby", "no-session is not on standby"])
        self.assertEqual(self.state_marks(), {"alpha": "run-finished"})

    def test_ready_refuses_an_unknown_showrunner_and_one_whose_timer_checks_no_doc(self) -> None:
        self.instance("showrunner-show")
        for showrunner, reason in (("show", "not a production doc: none registered"),
                                   ("absent", "no showrunner with an update timer is called absent")):
            with self.subTest(showrunner=showrunner):
                result = self.cli("ready", showrunner, "--unit", "alpha")
                self.assertEqual(result.returncode, 1)
                self.assertIn(reason, result.stderr)

    def test_status_marks_the_tmux_session_the_unit_calls_from(self) -> None:
        self.unit_session("alpha", "%4")
        self.unit_session("beta", "%5")
        _ = self.successful("status", "--state", "run-finished", pane="%4")
        _ = self.successful("status", "--state", "run-finished", pane="%4")
        self.assertEqual(self.state_marks(), {"alpha": "run-finished", "beta": ""})
        # A unit that started before the change still names the showrunner and itself; both are unused.
        _ = self.successful("status", "director", "--unit", "any-name", "--state", "running", pane="%4")
        self.assertEqual(self.state_marks(), {"alpha": "running", "beta": ""})
        self.assertFalse(self.config.exists())

    def test_status_refuses_a_caller_that_is_not_in_a_marked_unit_session(self) -> None:
        sessions = {"$1": FakeSession(label="not-a-unit", panes=["%9"], env={})}
        fake_tmux.write(self.tmux, sessions)
        for pane, reason in (("", "this is not one"), ("%9", "is not a marked unit session")):
            with self.subTest(pane=pane):
                result = self.cli("status", "--state", "run-finished", pane=pane)
                self.assertEqual(result.returncode, 1)
                self.assertIn(reason, result.stderr)
        self.assertEqual(fake_tmux.read(self.tmux), sessions)

    def test_settings_ignore_a_showrunner_list_stored_before_showrunners_were_looked_up(self) -> None:
        document = {**showrunners.defaults(), "showrunners": [{
            "session": "director", "zone": LOS_ANGELES, "units": [{"session": "finished", "status": "run-finished"}]}]}
        _ = self.config.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(showrunners.load_settings_from(self.config), showrunners.defaults())

    def test_an_absent_or_invalid_settings_file_is_one_error_that_names_the_file(self) -> None:
        for contents in (None, "{invalid json", '{"always": "wrong"}'):
            with self.subTest(contents=contents):
                self.config.unlink(missing_ok=True)
                if contents is not None:
                    _ = self.config.write_text(contents)
                with self.assertRaisesRegex(ValueError, re.escape(str(self.config))):
                    _ = showrunners.load_settings_from(self.config)


if __name__ == "__main__":
    _ = unittest.main()
