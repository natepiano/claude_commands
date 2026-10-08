"""The machine-local showrunner registry and its notifier import."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

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

    def record(self, name: str, session_id: str, *, running: bool = True, pid: int | None = None) -> None:
        if pid is None:
            child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.children.append(child)
            pid = child.pid
        _ = (self.sessions / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "name": name, "sessionId": session_id,
            "messagingSocketPath": str(self.root / f"{session_id}.sock"),
            "updatedAt": 1, "running": running,
        }))

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

    def entries(self) -> list[dict[str, object]]:
        content = cast(dict[str, object], json.loads(self.config.read_text()))
        return cast(list[dict[str, object]], content["showrunners"])

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

    def test_add_creates_defaults_then_sets_zone_and_holds_only_the_doc(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        content = cast(dict[str, object], json.loads(self.config.read_text()))
        self.assertEqual({key: content[key] for key in ("threshold_percent", "repeat_minutes",
                                                       "stall_minutes", "faults_to", "always")},
                         {"threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5,
                          "faults_to": "natedev", "always": ["natedev"]})
        # A later call that names no doc keeps the one held.
        _ = self.successful("add", "director", "--zone", "America/New_York")
        self.assertEqual(self.entries(), [{"session": "director", "zone": "America/New_York",
                                           "doc": str(self.doc)}])
        self.assertTrue((self.config.parent / "showrunners.lock").exists())

    def test_add_refuses_an_unknown_zone_without_changing_the_registry(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        before = self.config.read_bytes()
        result = self.cli("add", "director", "--zone", "Mars/Olympus", "--doc", str(self.doc))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.config.read_bytes(), before)

    def test_remove_is_idempotent(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        _ = self.successful("remove", "director")
        _ = self.successful("remove", "director")
        self.assertEqual(self.entries(), [])

    def test_unreadable_session_records_are_an_error_not_a_stopped_showrunner(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES)
        self.assertIn("director\tnot running", self.successful("list"))
        _ = self.sessions_script.write_text(
            "import sys\nprint('sessions: one or more registry files could not be read', file=sys.stderr)\n"
            + "raise SystemExit(3)\n")
        result = self.cli("list")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("not running", result.stdout)
        self.assertIn("cannot tell whether director is running: sessions: one or more registry files", result.stderr)

    def test_ready_marks_a_standing_by_unit_running_and_leaves_the_registry_alone(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        before = self.config.read_bytes()
        self.unit_session("alpha", "%4", "standing-by")
        self.unit_session("beta", "%5", "standing-by")
        _ = self.successful("ready", "director", "--unit", "alpha")
        self.assertEqual(self.state_marks(), {"alpha": "running", "beta": "standing-by"})
        self.assertEqual(self.config.read_bytes(), before)

    def test_ready_non_standby_unit_says_so_without_changing_its_mark(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        self.unit_session("alpha", "%4", "run-finished")
        result = self.cli("ready", "director", "--unit", "alpha", "--unit", "no-session")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout.splitlines(), ["alpha is not on standby", "no-session is not on standby"])
        self.assertEqual(self.state_marks(), {"alpha": "run-finished"})

    def test_ready_refuses_a_showrunner_with_no_registered_doc(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES)
        result = self.cli("ready", "director", "--unit", "alpha")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a production doc: none registered", result.stderr)

    def test_old_layout_is_read_and_its_unit_list_is_kept_through_a_write_for_adopt(self) -> None:
        document = {**showrunners.defaults(), "showrunners": [{
            "session": "director", "zone": LOS_ANGELES,
            "units": ["working", {"session": "finished", "status": "run-finished"}], "standby": ["waiting"],
        }]}
        _ = self.config.write_text(json.dumps(document), encoding="utf-8")
        self.assertIn("director\tnot running\tAmerica/Los_Angeles\t<no doc registered>", self.successful("list"))
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        self.assertEqual(self.entries(), [{
            "session": "director", "zone": LOS_ANGELES, "doc": str(self.doc),
            "units": ["working", {"session": "finished", "status": "run-finished"}]}])

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

    def test_list_shows_each_registered_doc(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        _ = self.successful("add", "other", "--zone", LOS_ANGELES)
        listed = self.successful("list").splitlines()
        self.assertEqual(listed, [f"director\tnot running\t{LOS_ANGELES}\t{self.doc}",
                                  f"other\tnot running\t{LOS_ANGELES}\t<no doc registered>"])

    def test_import_takes_the_doc_its_check_names_and_keeps_a_held_doc_when_it_names_none(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/New_York", "--doc", str(self.doc))
        self.record("director", "live-id")
        self.instance("showrunner-live")
        _ = self.successful("import")
        self.assertEqual(self.entries(), [{"session": "director", "zone": LOS_ANGELES, "doc": str(self.doc)}])
        shutil.rmtree(self.notifier / "showrunner-live")
        moved = self.root / "moved-production.md"
        self.instance("showrunner-live", doc=moved)
        _ = self.successful("import")
        self.assertEqual(self.entries(), [{"session": "director", "zone": LOS_ANGELES, "doc": str(moved)}])

    def test_concurrent_adds_both_land(self) -> None:
        first = subprocess.Popen([sys.executable, str(SCRIPT), "add", "first", "--zone", "America/Los_Angeles"],
                                 env=self.environment(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen([sys.executable, str(SCRIPT), "add", "second", "--zone", "America/New_York"],
                                  env=self.environment(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        first_out, first_err = first.communicate(timeout=10)
        second_out, second_err = second.communicate(timeout=10)
        self.assertEqual((first.returncode, second.returncode), (0, 0),
                         (first_out, first_err, second_out, second_err))
        self.assertEqual({entry["session"] for entry in self.entries()}, {"first", "second"})

    def test_import_reads_only_live_update_instances_and_skips_incomplete_ones(self) -> None:
        self.record("director", "live-id")
        self.record("offline", "offline-id", running=False, pid=101)
        self.instance("showrunner-live")
        self.instance("showrunner-offline", target="session:offline-id", units=("offline-unit",))
        self.instance("stall-watch", units=("ignored-unit",))
        self.instance("showrunner-no-target", include_target=False)
        self.instance("showrunner-no-prompt", include_prompt=False)
        self.instance("showrunner-no-zone", zone="")
        result = self.cli("import")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entries(), [{"session": "director", "zone": "America/Los_Angeles", "doc": ""}])
        self.assertEqual(len(result.stderr.splitlines()), 3, result.stderr)

    def test_live_instance_without_prompt_is_missing_but_import_skips_it(self) -> None:
        _ = self.successful("add", "configured", "--zone", "America/Los_Angeles")
        self.record("unconfigured", "live-id")
        for slug, change, reason in (("absent", "absent", "PROMPT_FILE is missing"),
                                     ("unreadable", "unreadable", "prompt could not be read"),
                                     ("malformed", "malformed", "unit_status.sh command is missing")):
            with self.subTest(slug=slug):
                name = f"showrunner-{slug}"
                self.instance(name, include_prompt=change != "absent")
                if change == "unreadable":
                    (self.notifier / name / "prompt").unlink()
                elif change == "malformed":
                    _ = (self.notifier / name / "prompt").write_text("No status command here.\n")
                listed = self.successful("list")
                self.assertIn(f"missing: {name}\tunconfigured", listed)
                imported = self.cli("import")
                self.assertEqual(imported.returncode, 0, imported.stderr)
                self.assertEqual(self.entries(), [{"session": "configured", "zone": "America/Los_Angeles",
                                                   "doc": ""}])
                self.assertIn(f"skipping {name}: {reason}", imported.stderr)
                shutil.rmtree(self.notifier / name)

    def test_rename_changes_a_showrunner_session_under_lock(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        _ = self.successful("rename", "director", "new director")
        self.assertEqual(self.entries(), [{"session": "new director", "zone": LOS_ANGELES, "doc": str(self.doc)}])
        self.assertTrue((self.config.parent / "showrunners.lock").exists())

    def test_rename_of_a_name_the_registry_does_not_hold_changes_nothing(self) -> None:
        _ = self.successful("add", "director", "--zone", LOS_ANGELES, "--doc", str(self.doc))
        before = self.config.read_bytes()
        self.instance("showrunner-live")
        prompt = (self.notifier / "showrunner-live" / "prompt").read_bytes()
        _ = self.successful("rename", "hook", "new-hook")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual((self.notifier / "showrunner-live" / "prompt").read_bytes(), prompt)

    def test_rename_updates_new_form_showrunner_argument(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles")
        self.instance("showrunner-live")
        prompt = self.notifier / "showrunner-live" / "prompt"
        _ = prompt.write_text("Run `zsh ~/.claude/scripts/production/unit_status.sh "
                              + "/tmp/status America/Los_Angeles --showrunner director | cut -c1-400`.\n"
                              + "Keep director in this note.\n")
        _ = self.successful("rename", "director", "new-director")
        self.assertEqual(self.entries()[0]["session"], "new-director")
        self.assertIn("--showrunner new-director |", prompt.read_text())
        self.assertIn("Keep director in this note.", prompt.read_text())

    def test_import_prefers_live_pid_record_over_stale_record_for_same_session(self) -> None:
        self.record("old name", "live-id", running=False, pid=999999)
        self.record("current name", "live-id")
        self.instance("showrunner-live")
        _ = self.successful("import")
        self.assertEqual(self.entries()[0]["session"], "current name")

    def test_import_rejects_session_with_no_live_pid_record(self) -> None:
        self.record("stale name", "live-id", pid=999999)
        self.instance("showrunner-live")
        result = self.cli("import")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entries(), [])
        self.assertIn("no live named process", result.stderr)

    def test_list_marks_running_and_reports_live_unconfigured_instance(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles")
        _ = self.successful("add", "offline", "--zone", "America/New_York")
        self.record("director", "live-id")
        self.record("offline", "offline-id", running=False, pid=101)
        self.record("missing", "missing-id")
        self.instance("showrunner-missing", target="session:missing-id", units=("extra-unit",))
        listed = self.successful("list")
        director = next(line for line in listed.splitlines() if line.startswith("director"))
        offline = next(line for line in listed.splitlines() if line.startswith("offline"))
        self.assertIn("running", director)
        self.assertNotIn("not running", director)
        self.assertIn("not running", offline)
        self.assertIn("missing:", listed)
        self.assertIn("showrunner-missing", listed)

    def test_list_absent_or_invalid_file_exits_one_with_one_diagnostic(self) -> None:
        for contents in (None, "{invalid json", '{"showrunners": "wrong"}'):
            with self.subTest(contents=contents):
                self.config.unlink(missing_ok=True)
                if contents is not None:
                    _ = self.config.write_text(contents)
                result = self.cli("list")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertIn(str(self.config), result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
