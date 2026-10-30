"""Showrunners read from their update timers, their names looked up at each call."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_unit_host
import showrunners
from sessions import SessionRecord
from fake_unit_host import HostedUnit
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase
from scripts.production.rename_test_support import bind_staff_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


SCRIPT = Path(__file__).with_name("showrunners.py")
LOS_ANGELES = "America/Los_Angeles"


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class ShowrunnerFixture(StaffTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.config: Path = Path()
        self.notifier: Path = Path()
        self.sessions: Path = Path()
        self.sessions_script: Path = Path()
        self.hosted_units: list[HostedUnit] = []
        self.doc: Path = Path()
        self.state: Path = Path()
        self.send_script: Path = Path()
        self.sent: Path = Path()
        self.children: list[subprocess.Popen[bytes]] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "config" / "showrunners.json"
        self.config.parent.mkdir()
        self.notifier = self.root / "notifier"
        self.notifier.mkdir()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.sessions_script = self.root / "sessions.py"
        self.hosted_units = []
        _ = fake_unit_host.environment(self.root)
        self.doc = self.root / "show-production.md"
        self.state = self.root / "showrunner-state"
        self.sent = self.root / "sent.jsonl"
        self.send_script = self.root / "send.py"
        _ = self.send_script.write_text("""import json, os, sys
with open(os.environ['TEST_SENT'], 'a') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
raise SystemExit(int(os.environ.get('TEST_SEND_EXIT', '0')))
""")
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
        bind_staff_fixture(self, self.environment(), {})

    def close_processes(self) -> None:
        for child in self.children:
            child.terminate()
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def record(self, name: str, session_id: str, *, running: bool = True, pid: int | None = None) -> int:
        if pid is None:
            child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.children.append(child)
            pid = child.pid
        for unit in self.hosted_units:
            if name in (unit.session, unit.marks.get("SHOWRUNNER_UNIT_ID"), unit.marks.get("SHOWRUNNER_UNIT_ID", "").removesuffix("-unit")):
                fake_unit_host.place_process(self.environment(), pid, unit.pane)
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
            # Older docs carry a User zone line. It is no longer read; the zone is the user's setting.
            _ = Path(doc).write_text(f"- **User zone:** {zone}\n", encoding="utf-8")
        _ = (directory / "conf").write_text("\n".join(fields) + "\n")

    def environment(self, pane: str = "", caller: str = "") -> dict[str, str]:
        # Keep the caller identity and host observations isolated from the running suite.
        return {**os.environ, "SHOWRUNNERS_CONFIG": str(self.config),
                "NOTIFIER_STATE_DIR": str(self.notifier), "NOTIFIER_SESSIONS_DIR": str(self.sessions),
                "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
                "SHOWRUNNER_STATE_DIR": str(self.state), "SHOWRUNNERS_SEND": str(self.send_script),
                "TEST_SENT": str(self.sent), "CLAUDE_CODE_SESSION_ID": caller,
                **fake_unit_host.environment(self.root), "HERDR_PANE_ID": pane}

    @override
    def cli(self, *args: str, pane: str = "", caller: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
        del code
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.environment(pane, caller),
                              capture_output=True, text=True, check=False)

    def successful(self, *args: str, pane: str = "", caller: str = "") -> str:
        result = self.cli(*args, pane=pane, caller=caller)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout

    def refused(self, *args: str, caller: str = "", code: int = 1) -> str:
        result = self.cli(*args, caller=caller)
        self.assertEqual(result.returncode, code, (args, result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        return result.stderr

    def showrunner(self, slug: str, *, running: bool = True, formerly: tuple[str, ...] = (), name: str = "") -> None:
        """One production with an update timer, its showrunner named `name`, else its slug, when it runs."""
        doc = self.root / f"{slug}-production.md"
        self.instance(f"showrunner-{slug}", target=f"session:{slug}-id", doc=doc)
        if formerly:
            names = ", ".join(f"`{old}`" for old in formerly)
            _ = doc.write_text(f"# Production\n\n## Production Context\n\n- **Formerly:** {names}\n",
                               encoding="utf-8")
        if running:
            _ = self.record(name or slug, f"{slug}-id")
        _ = staff.ensure("showrunner", name=slug)

    def messages(self) -> list[tuple[str, str]]:
        """Each message sent, as the socket it went to and its text."""
        if not self.sent.exists():
            return []
        sent = [cast(list[str], json.loads(line)) for line in self.sent.read_text().splitlines()]
        return [(one[one.index("--to") + 1], one[one.index("--text") + 1]) for one in sent]

    def stored_parent(self, child: str, parent: str) -> None:
        """Write a parent file by hand, as `attach` never would for a missing parent or a cycle."""
        (self.state / child).mkdir(parents=True, exist_ok=True)
        _ = (self.state / child / "parent").write_text(f"{parent}\n")

    def recorded_parent(self, child: str, parent: str) -> None:
        """Prepare authoritative edges alongside the legacy files still written by tools."""
        record = staff.initial_snapshot()
        parent_row = record.by_path(parent) or staff.ensure("showrunner", name=parent)
        child_row = record.by_path(child) or staff.ensure("showrunner", name=child)
        _ = staff.move(child_row.id, parent_row.id)
        self.stored_parent(child, parent)

    def unit_session(self, unit: str, pane: str) -> None:
        """Give the production `show` one marked herdr pane."""
        marks = {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": unit}
        self.hosted_units.append(HostedUnit(f"label-of-{unit}", pane, marks))
        fake_unit_host.write(self.environment(), self.hosted_units)


class ShowrunnerRegistryTests(ShowrunnerFixture):
    def test_adoption_retries_a_partial_file_or_directory_copy(self) -> None:
        for name, directory in (("unit_status", True), ("showrunner_state.json", False)):
            with self.subTest(name=name), mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.state):
                scratch = self.root / f"scratch-{name}"
                scratch.mkdir()
                source = scratch / name
                if directory:
                    source.mkdir()
                    _ = (source / "first").write_text("first state")
                    _ = (source / "last").write_text("last state")
                else:
                    _ = source.write_text('{"complete": true}\n')

                def interrupted_copy(source: Path, destination: Path) -> None:
                    if source.is_dir():
                        destination.mkdir()
                        _ = (destination / "first").write_text("partial")
                    else:
                        _ = destination.write_text("partial")
                    raise InterruptedError("copy stopped")

                with mock.patch.object(shutil, "copytree" if directory else "copy2",
                                       side_effect=interrupted_copy), self.assertRaises(InterruptedError):
                    _ = showrunners.copy_session_state(name, scratch)
                copied = showrunners.copy_session_state(name, scratch)
                self.assertEqual(copied, [name])
                target = showrunners.session_state_dir(name) / name
                if directory:
                    self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()},
                                     {p.name: p.read_bytes() for p in source.iterdir()})
                else:
                    self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(sorted(p.name for p in target.parent.iterdir()), [name])
                self.assertEqual(showrunners.copy_session_state(name, scratch), [])

    def test_checked_doc_reads_absolute_path_after_check_script(self) -> None:
        directory = self.notifier / "showrunner-check"
        directory.mkdir()
        doc = self.root / "example-production.md"
        check = shlex.join(["zsh", "/opt/tools/production_check.sh", str(doc), "extra"])
        _ = (directory / "conf").write_text(f"TARGET=session:abc\nCHECK={check}\n", encoding="utf-8")
        self.assertEqual(showrunners.checked_doc(directory), showrunners.CheckedDoc(doc))

    def test_registered_showrunners_uses_supplied_records_without_session_discovery(self) -> None:
        self.instance("showrunner-builds", target="session:builds-id", doc=self.doc)
        self.instance("showrunner-stopped", target="session:stopped-id", doc=self.root / "stopped.md")
        socket_path = Path(self.enterContext(short_socket_directory())) / "builds.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        self.addCleanup(listener.close)
        record: SessionRecord = {
            "pid": os.getpid(), "sessionId": "builds-id", "name": "builds", "updatedAt": 1,
            "messagingSocketPath": str(socket_path), "tmux": "", "formerNames": [],
        }
        with mock.patch.object(showrunners, "socket_for", side_effect=AssertionError("no sessions subprocess")) as sockets, \
             mock.patch.object(showrunners, "_session_name", side_effect=AssertionError("no session reread")) as names:
            found = showrunners.registered_showrunners(self.notifier, known_sessions={"builds-id": record})
            record["name"] = "renamed-builds"
            renamed = showrunners.registered_showrunners(self.notifier, known_sessions={"builds-id": record})
            absent = showrunners.registered_showrunners(self.notifier, known_sessions={})
        self.assertEqual([(runner["slug"], runner["session"], runner["socket"], runner["doc"]) for runner in found], [
            ("builds", "builds", str(socket_path), str(self.doc)),
            ("stopped", "", "", str(self.root / "stopped.md")),
        ])
        self.assertEqual(renamed[0]["session"], "renamed-builds")
        self.assertEqual([(runner["session"], runner["socket"]) for runner in absent], [("", ""), ("", "")])
        sockets.assert_not_called()
        names.assert_not_called()

    def test_an_unreadable_timer_conf_refuses_registry_discovery_beside_a_good_timer(self) -> None:
        self.instance("showrunner-a-good", target="session:good-id", doc=self.doc)
        self.instance("showrunner-z-bad", target="session:bad-id", doc=self.root / "bad-production.md")
        conf = self.notifier / "showrunner-z-bad" / "conf"
        conf.unlink()
        conf.mkdir()
        with self.assertRaises(IsADirectoryError) as raised:
            _ = showrunners.registered_showrunners(self.notifier, known_sessions={})
        self.assertIn(str(conf), str(raised.exception))

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
            # A timer whose check names no production doc is not a showrunner's: it is left out.
            f"show\tfirst-name\t{LOS_ANGELES}\t{self.doc}"])
        self.assertEqual(self.successful("name", "show"), "first-name\n")
        # A unit messages the showrunner's session on this machine, whoever else has its name.
        self.assertEqual(self.successful("address", "show"), f"uds:{self.root / 'live-id.sock'}\n")
        # A rename is written in one place, the session's own record, and every reader sees it.
        _ = self.record("renamed", "live-id", pid=pid)
        self.assertIn("show\trenamed\t", self.successful("list"))
        self.assertEqual(self.successful("name", "show"), "renamed\n")
        self.assertFalse(self.config.exists())

    def test_a_stopped_timer_still_names_its_showrunner(self) -> None:
        # A production on demand keeps its timer stopped, and the timer is still the showrunner's record.
        self.instance("showrunner-show", doc=self.doc)
        _ = (self.notifier / "showrunner-show" / "state").write_text(
            "ENABLED=0\nNEXT_DUE=0\nLAST_SENT=0\nLAST_RESTART=0\nLAST_TARGET=\n", encoding="utf-8")
        _ = self.record("on-demand", "live-id")
        self.assertEqual(self.successful("name", "show"), "on-demand\n")
        self.assertIn("show\ton-demand\t", self.successful("list"))

    def test_former_slug_reaches_the_current_showrunner_by_name_and_address(self) -> None:
        self.showrunner("new", formerly=("old",), name="new-name")
        address = f"uds:{self.root / 'new-id.sock'}"
        self.assertEqual(self.successful("address", "old"), f"{address}\n")
        self.assertEqual(self.successful("name", "old"), "new-name\n")
        with (mock.patch.dict(os.environ, self.environment()),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(showrunners, "SESSIONS_DIR", self.sessions),
              mock.patch.object(showrunners, "SESSIONS", self.sessions_script)):
            self.assertEqual(showrunners.current_address("old"), address)
            self.assertEqual(showrunners.current_name("old"), "new-name")

    def test_named_accepts_a_live_session_name_and_a_former_production_slug(self) -> None:
        self.showrunner("new", formerly=("old",), name="new-name")
        with (mock.patch.dict(os.environ, self.environment()),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(showrunners, "SESSIONS_DIR", self.sessions),
              mock.patch.object(showrunners, "SESSIONS", self.sessions_script)):
            self.assertEqual(showrunners.named("new-name")["slug"], "new")
            self.assertEqual(showrunners.named("old")["slug"], "new")

    def test_named_prefers_the_production_a_former_slug_resolves_to_over_a_session_of_that_name(self) -> None:
        # The session-named runner sorts first, so a first-match lookup would return it.
        self.instance("showrunner-a-other", target="session:a-other-id", doc=self.root / "a-other-production.md")
        _ = self.record("old", "a-other-id")
        self.showrunner("z-new", formerly=("old",))
        with (mock.patch.dict(os.environ, self.environment()),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(showrunners, "SESSIONS_DIR", self.sessions),
              mock.patch.object(showrunners, "SESSIONS", self.sessions_script)):
            self.assertEqual(showrunners.named("old")["slug"], "z-new")

    def test_a_formerly_line_outside_production_context_claims_no_slug(self) -> None:
        self.showrunner("new", running=False)
        _ = (self.root / "new-production.md").write_text(
            "# Production\n\n## Production Context\n\n- **Owner:** someone\n\n## Notes\n\n```\n- **Formerly:** `old`\n```\n",
            encoding="utf-8")
        self.assertEqual(showrunners.canonical_slug("old", self.notifier), "old")

    def test_formerly_can_name_two_previous_slugs_without_a_running_showrunner(self) -> None:
        self.showrunner("new", running=False, formerly=("old", "older"))
        for slug in ("new", "old", "older"):
            with self.subTest(slug=slug):
                self.assertEqual(showrunners.canonical_slug(slug, self.notifier), "new")
        self.assertEqual(showrunners.production_slug(str(self.root / "new-production.md")), "new")

    def test_a_current_slug_takes_precedence_over_another_productions_former_slug(self) -> None:
        # The former owner sorts first, so an alias match cannot win before all current slugs are checked.
        self.showrunner("a-renamed", formerly=("z-current",))
        self.showrunner("z-current")
        self.assertEqual(showrunners.canonical_slug("z-current", self.notifier), "z-current")
        self.assertEqual(self.successful("address", "z-current"), f"uds:{self.root / 'z-current-id.sock'}\n")

    def test_an_unknown_slug_passes_through_but_showrunner_commands_refuse_it(self) -> None:
        self.showrunner("new", running=False, formerly=("old",))
        self.assertEqual(showrunners.canonical_slug("older", self.notifier), "older")
        for verb in ("name", "address"):
            with self.subTest(verb=verb):
                self.assertIn("the showrunner of older is not running", self.refused(verb, "older"))
        for arguments in (("attach", "older", "new"), ("parent", "older")):
            with self.subTest(arguments=arguments):
                self.assertIn("older has no update timer", self.refused(*arguments))
        self.assertFalse(self.state.exists())

    def test_name_and_address_refuse_a_showrunner_that_is_not_running_or_has_no_timer(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        for verb in ("name", "address"):
            for slug in ("show", "absent"):
                with self.subTest(verb=verb, slug=slug):
                    result = self.cli(verb, slug)
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

    def test_the_zone_is_the_users_setting_whatever_the_doc_says(self) -> None:
        # A doc's own User zone line is no longer read: a doc without one, or with one that names
        # another zone or no zone at all, is listed with the setting's zone.
        no_zone = self.root / "no-zone-production.md"
        self.instance("showrunner-no-zone", doc=no_zone)
        _ = no_zone.write_text("# Production\n", encoding="utf-8")
        other_zone = self.root / "other-zone-production.md"
        self.instance("showrunner-other-zone", doc=other_zone, zone="Europe/Paris")
        bad_zone = self.root / "bad-zone-production.md"
        self.instance("showrunner-bad-zone", doc=bad_zone, zone="Nowhere/Never")
        _ = self.record("director", "live-id")
        result = self.cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()), sorted(
            f"{slug}\tdirector\t{LOS_ANGELES}\t{doc}"
            for slug, doc in (("no-zone", no_zone), ("other-zone", other_zone), ("bad-zone", bad_zone))))
        self.assertNotIn("skipping", result.stderr)

    def test_a_timer_that_is_not_a_complete_showrunners_is_left_out_with_the_reason(self) -> None:
        self.instance("showrunner-no-target", include_target=False)
        self.instance("showrunner-no-doc")
        self.instance("showrunner-unnamed", target="session:unnamed-id", doc=self.root / "unnamed-production.md")
        self.instance("showrunner-lost-doc", doc=self.root / "lost-production.md")
        (self.root / "lost-production.md").unlink()
        self.instance("showrunner-show", doc=self.doc)
        _ = self.record("director", "live-id")
        # A session that answers on its socket and has no live named process cannot be named.
        _ = self.record("gone", "unnamed-id", pid=99999999)
        result = self.cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"show\tdirector\t{LOS_ANGELES}\t{self.doc}"])
        self.assertIn("skipping showrunner-no-target", result.stderr)
        self.assertIn("skipping showrunner-no-doc: CHECK is missing", result.stderr)
        self.assertIn("skipping showrunner-lost-doc: production doc could not be read", result.stderr)
        self.assertIn("skipping showrunner-unnamed: session unnamed-id has no live named process", result.stderr)

    def test_the_name_comes_from_the_live_process_not_a_stale_record_of_the_same_session(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        _ = self.record("stale-name", "live-id", pid=99999999)
        _ = self.record("director", "live-id")
        self.assertEqual(self.successful("name", "show"), "director\n")

    def test_the_old_ready_and_status_commands_do_nothing_and_succeed(self) -> None:
        # Sessions started before a unit's run state was read from its run records still call them.
        self.unit_session("alpha", "w4:p1")
        before = (self.root / "herdr-responses.json").read_bytes()
        for arguments in (("ready", "director", "--unit", "alpha"), ("ready", "no-such-showrunner", "--unit", "x"),
                          ("status", "--state", "run-finished"),
                          ("status", "director", "--unit", "any-name", "--state", "running")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.successful(*arguments, pane="w4:p1"), "")
        self.assertEqual((self.root / "herdr-responses.json").read_bytes(), before)

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

    def test_delivery_result_tracks_failed_targets_and_preserves_output_lines(self) -> None:
        def address(slug: str) -> str:
            return "session:" + slug

        with mock.patch.object(showrunners, "own_slug", return_value="self"), \
                mock.patch.object(showrunners, "current_address", side_effect=address), \
                mock.patch.object(showrunners, "send_to_address", wraps=showrunners.send_to_address) as sender, \
                mock.patch.object(subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 3, "", "offline")]):
            delivery = showrunners.tell_others({"self": "skip", "top": "hello", "mid": "hello"}, "notice")
        self.assertEqual(list(delivery.lines), ["told top", "could not tell mid: offline"])
        self.assertEqual(delivery.failed, ("mid",))
        self.assertEqual(sender.call_args_list, [mock.call("session:top", "notice", "hello", recipient="top"),
                                                 mock.call("session:mid", "notice", "hello", recipient="mid")])
        with self.assertRaises(FrozenInstanceError):
            setattr(delivery, "lines", ())
        with self.assertRaises(FrozenInstanceError):
            setattr(delivery, "failed", ())

    def test_public_address_sender_preserves_recipient_sender_key_and_text(self) -> None:
        with mock.patch.dict(os.environ, self.environment()), \
                mock.patch.object(showrunners, "SEND", self.send_script):
            delivery = showrunners.send_to_address("session:unit-id", "transfer-key", "new production\nkeep running",
                                                   recipient="alpha-unit", sender="transfer_unit")
        self.assertEqual(delivery, showrunners.NoticeDelivery(("told alpha-unit",)))
        sent = [json.loads(line) for line in self.sent.read_text().splitlines()]
        self.assertEqual(sent, [["--to", "session:unit-id", "--from", "transfer_unit", "--key", "transfer-key",
                                 "--text", "new production\nkeep running"]])


class HerdrNameTests(unittest.TestCase):
    def test_workspace_is_named_for_the_showrunner(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        named, unnamed = root / "prod-production.md", root / "other-production.md"
        _ = named.write_text("# Prod\n\n- **Showrunner checkout:** `/repo`\n- **Showrunner session:** boss\n")
        _ = unnamed.write_text("# Other\n")
        self.assertEqual(showrunners.herdr_space("prod", str(named)), "boss")
        self.assertEqual(showrunners.herdr_space("other", str(unnamed)), "other")
        self.assertEqual(showrunners.herdr_space("gone", str(root / "gone-production.md")), "gone")

if __name__ == "__main__":
    _ = unittest.main()
