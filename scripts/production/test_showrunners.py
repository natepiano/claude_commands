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
from unittest import mock

import showrunners


SCRIPT = Path(__file__).with_name("showrunners.py")


class ShowrunnerRegistryTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config: Path = Path()
        self.notifier: Path = Path()
        self.sessions: Path = Path()
        self.sessions_script: Path = Path()
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
                 include_target: bool = True, include_prompt: bool = True) -> None:
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
        _ = (directory / "conf").write_text("\n".join(fields) + "\n")

    def environment(self) -> dict[str, str]:
        return {**os.environ, "SHOWRUNNERS_CONFIG": str(self.config),
                "NOTIFIER_STATE_DIR": str(self.notifier), "NOTIFIER_SESSIONS_DIR": str(self.sessions),
                "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
                "STALL_WATCH_STATE_DIR": str(self.root / "stall-state")}

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.environment(),
                              capture_output=True, text=True, check=False)

    def successful(self, *args: str) -> str:
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout

    def entries(self) -> list[dict[str, object]]:
        content = cast(dict[str, object], json.loads(self.config.read_text()))
        return cast(list[dict[str, object]], content["showrunners"])

    def unit_states(self) -> dict[str, str]:
        settings = showrunners.load_settings_from(self.config)
        return {unit.session: type(unit).__name__
                for runner in settings["showrunners"] for unit in runner["units"]}

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

    def test_add_creates_defaults_then_sets_zone_and_appends_only_new_units(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "hook")
        content = cast(dict[str, object], json.loads(self.config.read_text()))
        self.assertEqual({key: content[key] for key in ("threshold_percent", "repeat_minutes",
                                                       "stall_minutes", "faults_to", "always")},
                         {"threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5,
                          "faults_to": "natedev", "always": ["natedev"]})
        _ = self.successful("add", "director", "--zone", "America/New_York", "--unit", "hook",
                            "--unit", "organon")
        _ = self.successful("add", "director", "--zone", "America/New_York", "--unit", "organon")
        self.assertEqual(self.entries(), [{"session": "director", "zone": "America/New_York",
                                           "units": [{"session": "hook", "status": "running"},
                                                     {"session": "organon", "status": "running"}]}])
        self.assertTrue((self.config.parent / "showrunners.lock").exists())

    def test_add_refuses_an_empty_unit_without_changing_the_registry(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "alpha")
        before = self.config.read_bytes()

        result = self.cli("add", "director", "--zone", "America/New_York", "--unit", "")

        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid unit session", result.stderr)
        self.assertEqual(self.config.read_bytes(), before)

    def test_remove_selected_units_then_entire_showrunner_is_idempotent(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "hook",
                            "--unit", "organon")
        _ = self.successful("remove", "director", "--unit", "hook")
        _ = self.successful("remove", "director", "--unit", "hook")
        self.assertEqual(self.entries()[0]["units"], [{"session": "organon", "status": "running"}])
        _ = self.successful("remove", "director")
        _ = self.successful("remove", "director")
        self.assertEqual(self.entries(), [])

    def test_standby_add_list_and_ready_preserve_unit_membership(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles",
                            "--unit", "alpha", "--standby")
        self.assertEqual(self.entries()[0]["units"], [{"session": "alpha", "status": "standing-by"}])
        self.assertNotIn("standby", self.entries()[0])
        self.assertIn("alpha:standing-by", self.successful("list"))
        _ = self.successful("ready", "director", "--unit", "alpha")
        self.assertEqual(self.entries()[0]["units"], [{"session": "alpha", "status": "running"}])
        self.assertNotIn("alpha:standing-by", self.successful("list"))

    def test_ready_non_standby_unit_says_so_without_changing_config(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "alpha")
        before = self.config.read_bytes()
        result = self.cli("ready", "director", "--unit", "alpha")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
        self.assertIn("alpha", result.stdout + result.stderr)
        self.assertIn("not on standby", result.stdout + result.stderr)

    def test_old_layout_reads_standby_and_running_unit_directors(self) -> None:
        document = {**showrunners.defaults(), "showrunners": [{
            "session": "director", "zone": "America/Los_Angeles",
            "units": ["working", "waiting"], "standby": ["waiting"],
        }]}
        _ = self.config.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.unit_states(), {
            "working": "RunningUnitDirector",
            "waiting": "StandingByUnitDirector",
        })

    def test_status_is_idempotent_and_can_restart_a_finished_run(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "alpha")
        _ = self.successful("status", "director", "--unit", "alpha", "--state", "run-finished")
        finished = self.config.read_bytes()
        _ = self.successful("status", "director", "--unit", "alpha", "--state", "run-finished")
        self.assertEqual(self.config.read_bytes(), finished)
        _ = self.successful("status", "director", "--unit", "alpha", "--state", "running")
        self.assertEqual(self.unit_states(), {"alpha": "RunningUnitDirector"})

    def test_status_refuses_an_absent_unit(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "alpha")
        absent = self.cli("status", "director", "--unit", "missing", "--state", "run-finished")
        self.assertEqual(absent.returncode, 1)
        self.assertIn("unit is absent", absent.stderr)

    def test_status_refuses_an_ambiguous_unit(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "alpha")
        settings = showrunners.load_settings_from(self.config)
        settings["showrunners"][0]["units"].append(showrunners.StandingByUnitDirector("alpha"))
        with self.assertRaisesRegex(ValueError, "unit is ambiguous"):
            _ = showrunners.set_unit_status(settings, "director", showrunners.RunFinishedUnitDirector("alpha"))

    def test_status_refuses_an_ambiguous_showrunner(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "alpha")
        settings = showrunners.load_settings_from(self.config)
        settings["showrunners"].append(settings["showrunners"][0].copy())
        with self.assertRaisesRegex(ValueError, "showrunner is ambiguous"):
            _ = showrunners.set_unit_status(settings, "director", showrunners.RunFinishedUnitDirector("alpha"))

    def test_list_shows_each_registered_status(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles",
                            "--unit", "working", "--unit", "finished")
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles",
                            "--unit", "waiting", "--standby")
        _ = self.successful("status", "director", "--unit", "finished", "--state", "run-finished")
        listed = self.successful("list")
        self.assertIn("working:running", listed)
        self.assertIn("finished:run-finished", listed)
        self.assertIn("waiting:standing-by", listed)

    def test_add_and_import_keep_existing_unit_statuses(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/New_York",
                            "--unit", "working", "--unit", "finished")
        _ = self.successful("add", "director", "--zone", "America/New_York",
                            "--unit", "waiting", "--standby")
        _ = self.successful("status", "director", "--unit", "finished", "--state", "run-finished")
        _ = self.successful("add", "director", "--zone", "America/New_York",
                            "--unit", "finished", "--unit", "waiting")
        self.record("director", "live-id")
        self.instance("showrunner-live", units=("working", "finished", "waiting", "new"))
        _ = self.successful("import")
        self.assertEqual(self.unit_states(), {
            "working": "RunningUnitDirector",
            "finished": "RunFinishedUnitDirector",
            "waiting": "StandingByUnitDirector",
            "new": "RunningUnitDirector",
        })

    def test_concurrent_adds_both_land(self) -> None:
        first = subprocess.Popen([sys.executable, str(SCRIPT), "add", "first", "--zone", "America/Los_Angeles",
                                  "--unit", "hook"], env=self.environment(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen([sys.executable, str(SCRIPT), "add", "second", "--zone", "America/New_York",
                                   "--unit", "organon"], env=self.environment(), stdout=subprocess.PIPE,
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
        self.assertEqual(self.entries(), [{"session": "director", "zone": "America/Los_Angeles",
                                           "units": [
                                               {"session": "hook", "status": "running"},
                                               {"session": "tool-based-ui-geometry-material",
                                                "status": "running"},
                                           ]}])
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
                                                   "units": []}])
                self.assertIn(f"skipping {name}: {reason}", imported.stderr)
                shutil.rmtree(self.notifier / name)

    def test_rename_changes_unit_and_showrunner_session_under_lock(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "hook")
        _ = self.successful("rename", "hook", "new hook")
        _ = self.successful("rename", "director", "new director")
        self.assertEqual(self.entries(), [{"session": "new director", "zone": "America/Los_Angeles",
                                           "units": [{"session": "new hook", "status": "running"}]}])
        self.assertTrue((self.config.parent / "showrunners.lock").exists())

    def test_rename_keeps_all_three_unit_status_variants(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles",
                            "--unit", "working", "--unit", "finished")
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles",
                            "--unit", "waiting", "--standby")
        _ = self.successful("status", "director", "--unit", "finished", "--state", "run-finished")
        for old, new in (("working", "new-working"), ("finished", "new-finished"),
                         ("waiting", "new-waiting")):
            _ = self.successful("rename", old, new)
        self.assertEqual(self.unit_states(), {
            "new-working": "RunningUnitDirector",
            "new-finished": "RunFinishedUnitDirector",
            "new-waiting": "StandingByUnitDirector",
        })

    def test_failed_stall_rename_keeps_registry_and_retry_completes(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "hook")
        original = self.config.read_bytes()
        failed = subprocess.CompletedProcess(["stall_watch.py"], 1, "", "injected failure")
        succeeded = subprocess.CompletedProcess(["stall_watch.py"], 0, "", "")
        with mock.patch.object(showrunners, "CONFIG", self.config), \
                mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier), \
                mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(ValueError, "stall state rename failed"):
                showrunners.change("rename", "hook", "", [], "new-hook")
        self.assertEqual(self.config.read_bytes(), original)
        with mock.patch.object(showrunners, "CONFIG", self.config), \
                mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier), \
                mock.patch.object(subprocess, "run", return_value=succeeded):
            showrunners.change("rename", "hook", "", [], "new-hook")
        self.assertEqual(self.entries()[0]["units"], [{"session": "new-hook", "status": "running"}])

    def test_second_registry_rename_changes_nothing(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "hook")
        _ = self.successful("rename", "hook", "new-hook")
        renamed = self.config.read_bytes()
        with mock.patch.object(showrunners, "CONFIG", self.config), \
                mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier), \
                mock.patch.object(subprocess, "run") as run:
            showrunners.change("rename", "hook", "", [], "new-hook")
        self.assertEqual(self.config.read_bytes(), renamed)
        run.assert_not_called()

    def test_rename_updates_old_form_unit_lists_in_every_prompt(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "tool-based-ui-trunk")
        _ = self.successful("add", "other", "--zone", "America/Los_Angeles", "--unit", "tool-based-ui-trunk")
        for slug in ("showrunner-first", "showrunner-second"):
            self.instance(slug, units=("tool-based-ui-trunk", "tool-based-ui-trunk-extra"))
            prompt = self.notifier / slug / "prompt"
            _ = prompt.write_text(prompt.read_text() + "Keep tool-based-ui-trunk in this note.\n")
        _ = self.successful("rename", "tool-based-ui-trunk", "trunk")
        for slug in ("showrunner-first", "showrunner-second"):
            prompt = (self.notifier / slug / "prompt").read_text()
            self.assertIn("America/Los_Angeles trunk tool-based-ui-trunk-extra |", prompt)
            self.assertIn("Keep tool-based-ui-trunk in this note.", prompt)
        self.assertEqual([entry["units"] for entry in self.entries()],
                         [[{"session": "trunk", "status": "running"}],
                          [{"session": "trunk", "status": "running"}]])

    def test_rename_updates_new_form_showrunner_argument(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "hook")
        self.instance("showrunner-live")
        prompt = self.notifier / "showrunner-live" / "prompt"
        _ = prompt.write_text("Run `zsh ~/.claude/scripts/production/unit_status.sh "
                              + "/tmp/status America/Los_Angeles --showrunner director | cut -c1-400`.\n"
                              + "Keep director in this note.\n")
        _ = self.successful("rename", "director", "new-director")
        self.assertEqual(self.entries()[0]["session"], "new-director")
        self.assertIn("--showrunner new-director |", prompt.read_text())
        self.assertIn("Keep director in this note.", prompt.read_text())

    def test_import_new_prompt_updates_zone_without_adding_names_as_units(self) -> None:
        _ = self.successful("add", "director", "--zone", "America/New_York", "--unit", "existing")
        self.record("director", "live-id")
        self.instance("showrunner-live")
        prompt = self.notifier / "showrunner-live" / "prompt"
        _ = prompt.write_text("Run `zsh ~/.claude/scripts/production/unit_status.sh "
                              + "/tmp/run/unit_status America/Los_Angeles --showrunner director | cut -c1-400`.\n")
        _ = self.successful("import")
        self.assertEqual(self.entries(), [{"session": "director", "zone": "America/Los_Angeles",
                                           "units": [{"session": "existing", "status": "running"}]}])

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
        _ = self.successful("add", "director", "--zone", "America/Los_Angeles", "--unit", "hook")
        _ = self.successful("add", "offline", "--zone", "America/New_York", "--unit", "organon")
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
