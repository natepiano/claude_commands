"""Showrunners read from their update timers, their names looked up at each call."""

from __future__ import annotations

from scripts.production.staff_test_support import unit_membership, fixture_member, fixture_runtime_id

import json
import io
from contextlib import redirect_stderr, redirect_stdout
from time import monotonic
from dataclasses import FrozenInstanceError, replace
import os
import re
import socket
import subprocess
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.atomic_file import write_bytes_atomic, write_text_atomic


from scripts.production import fake_unit_host
from scripts.production import showrunners, staff_sources
from scripts.message.sessions import SessionRecord
from scripts.production.fake_unit_host import HostedUnit
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase
from scripts.production.rename_test_support import bind_staff_fixture


SCRIPT = Path(__file__).with_name("showrunners.py")
LOS_ANGELES = "America/Los_Angeles"


def stalled_session_reader(root: Path) -> Path:
    """An isolated sessions command that never answers; callers must bound its lifetime."""
    reader = root / "stalled-sessions.py"
    _ = reader.write_text("import threading\nthreading.Event().wait()\n")
    return reader


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
        self.session_labels: dict[str, list[str]] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "config" / "showrunners.conf"
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
        _ = self.enterContext(mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.state))
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
        socket_name = session_id
        try:
            session_id = staff.native_id(session_id)
        except staff.StaffRefused:
            session_id = fixture_runtime_id(session_id)
        for unit in self.hosted_units:
            if name == unit.session or session_id == unit.marks.get("CODEX_THREAD_ID"):
                fake_unit_host.place_process(self.environment(), pid, unit.pane)
        for index, runtime in enumerate(self.session_labels.get(session_id, [session_id])):
            runtime_pid = pid
            if index:
                child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.children.append(child)
                runtime_pid = child.pid
            path = self.sessions / f"{runtime_pid}.json"
            _ = path.write_text(json.dumps({
                "pid": runtime_pid, "name": name, "sessionId": runtime,
                "messagingSocketPath": str(self.root / f"{socket_name}.sock"),
                "updatedAt": 1, "running": running,
            }))
        return pid

    def instance(self, name: str, *, target: str = "session:live-id", zone: str = "America/Los_Angeles",
                 units: tuple[str, ...] = ("hook", "tool-based-ui-geometry-material"),
                 include_target: bool = True, include_prompt: bool = True, doc: Path | None = None) -> None:
        if name.startswith("showrunner-") and doc is not None:
            owner_name = name.removeprefix("showrunner-")
            row = staff.initial_snapshot().by_id(staff.StaffId(owner_name))
            row = row or next((row for row in staff.initial_snapshot().find(kind="showrunner") if row.name == owner_name), None)
            if row is None:
                row = fixture_member("showrunner", name=owner_name, doc=staff.LiveDoc("natedev", doc))
            current = target.removeprefix("session:")
            try:
                current = staff.native_id(current)
            except staff.StaffRefused:
                current = fixture_runtime_id(current)
            label = current
            existing = staff.initial_snapshot().by_current_session(current)
            if existing is not None and existing.id != row.id:
                current = fixture_runtime_id(row.id + ":" + current)
            self.session_labels.setdefault(label, []).append(current)
            if row.current_session != current:
                row = staff.update_current_session(row.current_session, staff.native_id(current))
            name = f"showrunner-{row.id}"
            target = f"session:{row.id}"
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
            fields.append(f"CHECK=zsh /opt/tools/production_check.sh {target.removeprefix('session:')}")
            # Older docs carry a User zone line. It is no longer read; the zone is the user's setting.
            _ = Path(doc).write_text(f"- **User zone:** {zone}\n", encoding="utf-8")
        _ = (directory / "conf").write_text("\n".join(fields) + "\n")

    def environment(self, pane: str = "", caller: str = "") -> dict[str, str]:
        # Keep the caller identity and host observations isolated from the running suite.
        return {**os.environ, "SHOWRUNNERS_CONFIG": str(self.config),
                "NOTIFIER_STATE_DIR": str(self.notifier), "NOTIFIER_SESSIONS_DIR": str(self.sessions),
                "SHOWRUNNERS_SESSIONS": str(self.sessions_script),
                "SHOWRUNNER_STATE_DIR": str(self.state), "SHOWRUNNERS_SEND": str(self.send_script),
                "TEST_SENT": str(self.sent), "CLAUDE_CODE_SESSION_ID": caller, "CODEX_THREAD_ID": caller,
                **fake_unit_host.environment(self.root), "HERDR_PANE_ID": pane}

    @override
    def cli(self, *args: str, pane: str = "", caller: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
        del code
        return subprocess.run([str(SCRIPT.parents[1] / "lib/py"), str(SCRIPT), *args], env=self.environment(pane, caller),
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

    def showrunner(self, production_name: str, *, running: bool = True, name: str = "") -> None:
        """One production with an update timer, its showrunner named `name`, else its production_name, when it runs."""
        doc = self.root / f"{production_name}-production.md"
        row = fixture_member("showrunner", name=production_name)
        self.instance(f"showrunner-{production_name}", target=f"session:{row.id}", doc=doc)
        _ = staff.set_doc(row.id, staff.LiveDoc("natedev", doc))
        if running:
            pid = self.record(name or production_name, row.id)
            path = self.sessions / f"{pid}.json"
            value = cast(dict[str, object], json.loads(path.read_text()))
            value["messagingSocketPath"] = str(self.root / f"{production_name}-id.sock")
            write_text_atomic(path, json.dumps(value) + "\n")

    def messages(self) -> list[tuple[str, str]]:
        """Each message sent, as the socket it went to and its text."""
        if not self.sent.exists():
            return []
        sent = [cast(list[str], json.loads(line)) for line in self.sent.read_text().splitlines()]
        return [(one[one.index("--to") + 1], one[one.index("--text") + 1]) for one in sent]

    def parent_receipt(self, child: str) -> Path:
        """Select the private owner's parent receipt."""
        row = staff.snapshot().by_path(child)
        assert row is not None
        return self.state / row.id / "parent"

    def stored_parent(self, child: str, parent: str) -> None:
        """Write a private parent receipt for missing-parent and cycle tests."""
        path = self.parent_receipt(child)
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(f"{parent}\n")

    def recorded_parent(self, child: str, parent: str) -> None:
        """Prepare authoritative edges alongside the legacy files still written by tools."""
        record = staff.initial_snapshot()
        parent_row = record.by_path(parent) or fixture_member("showrunner", name=parent)
        child_row = record.by_path(child) or fixture_member("showrunner", name=child)
        _ = staff.move(child_row.id, staff.ReportsTo(parent_row.id))
        self.stored_parent(child, parent)

    def unit_session(self, unit: str, pane: str) -> None:
        """Give the production `show` one marked herdr pane."""
        marks: dict[str, str] = {"CODEX_THREAD_ID": unit_membership("show", unit).current_session}
        self.hosted_units.append(HostedUnit(f"label-of-{unit}", pane, marks))
        fake_unit_host.write(self.environment(), self.hosted_units)


class RowTimerJoinTests(StaffTestCase):
    def register_timer(self, row: staff.StaffRow) -> Path:
        path = Path(os.environ["NOTIFIER_STATE_DIR"]) / f"showrunner-{row.id}" / "conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, f"TARGET=session:{row.id}\n")
        return path

    def test_unreadable_timer_does_not_hide_another_owner(self) -> None:
        fixture = self.row_production()
        root = Path(os.environ["NOTIFIER_STATE_DIR"])
        _ = self.register_timer(fixture.runner)
        bad_owner = fixture_member("showrunner", name="broken-owner")
        bad_conf = root / f"showrunner-{bad_owner.id}" / "conf"
        bad_conf.mkdir(parents=True)
        record = staff.snapshot()
        timer = showrunners.timer_for(fixture.runner, record)
        self.assertIsInstance(timer, showrunners.TimerRegistration)
        assert isinstance(timer, showrunners.TimerRegistration)
        self.assertEqual(timer.target, f"session:{fixture.runner.id}")
        with self.assertRaises(OSError) as raised:
            _ = showrunners.timer_for(bad_owner, record)
        self.assertIn(str(bad_conf), str(raised.exception))

    def test_timer_identity_does_not_depend_on_the_check_document(self) -> None:
        fixture = self.row_production()
        timer_file = self.register_timer(fixture.runner)
        write_text_atomic(timer_file, f"TARGET=session:{fixture.runner.id}\nCHECK=unavailable\n")
        before = self.staff_file.read_bytes()
        timer = showrunners.timer_for(fixture.runner, staff.snapshot())
        self.assertIsInstance(timer, showrunners.TimerRegistration)
        assert isinstance(timer, showrunners.TimerRegistration)
        self.assertEqual(timer.target, f"session:{fixture.runner.id}")
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_timer_uses_the_current_staff_name(self) -> None:
        fixture = self.row_production()
        _ = self.register_timer(fixture.runner)
        row = staff.rename(fixture.runner.id, "new-owner")
        before = self.staff_file.read_bytes()
        timer = showrunners.timer_for(row, staff.snapshot())
        self.assertIsInstance(timer, showrunners.TimerRegistration)
        assert isinstance(timer, showrunners.TimerRegistration)
        self.assertEqual(timer.name, "new-owner")
        self.assertEqual(timer.target, f"session:{row.id}")
        self.assertEqual(self.staff_file.read_bytes(), before)

    def timer(self, name: str) -> tuple[staff.StaffRow, Path]:
        doc = self.root / f"{name}-production.md"
        write_text_atomic(doc, "# Production\n")
        row = fixture_member("showrunner", name=name, doc=staff.LiveDoc("natedev", doc))
        folder = self.root / "timers" / f"showrunner-{row.id}"
        folder.mkdir(parents=True)
        write_text_atomic(folder / "conf", f"TARGET=session:{row.id}\nEVERY=17\nSOCKET=/tmp/socket\n"
                          + f"CHECK=zsh /tools/production_check.sh {row.id}\nZONE=UTC\n")
        return row, folder

    def test_timer_uses_the_supplied_staff_name(self) -> None:
        row, folder = self.timer("hana")
        row = staff.rename(row.id, "hana-owner")
        record = staff.snapshot()
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent),
              mock.patch.object(staff, "snapshot", side_effect=AssertionError("staff was read again"))):
            sources = showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.RAISE, record=record)
            runners = showrunners.registered_showrunners(known_sessions={}, membership=record)
            members = staff_sources.showrunner_members(record)
        self.assertEqual([source.timer.name for source in sources], ["hana-owner"])
        self.assertEqual([runner["name"] for runner in runners], ["hana-owner"])
        self.assertEqual(members, {"hana-owner": row})

    def test_remote_or_timerless_staff_remains_a_member(self) -> None:
        row, folder = self.timer("remote")
        remote = replace(row, doc=staff.LiveDoc("other-machine", self.root / "remote-production.md"))
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent):
            self.assertIsInstance(showrunners.timer_for(remote, staff.StaffSnapshot((remote,))),
                                  showrunners.TimerRegistration)
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "absent"):
            self.assertIsInstance(showrunners.timer_for(row, staff.snapshot()), showrunners.NoTimer)
        self.assertEqual(staff.snapshot().by_id(row.id), row)

    def test_timer_lookup_does_not_require_a_document(self) -> None:
        row = fixture_member("showrunner", name="without-document")
        folder = self.root / "timers" / f"showrunner-{row.id}"
        folder.mkdir(parents=True)
        write_text_atomic(folder / "conf", f"TARGET=session:{row.id}\nEVERY=17\n")
        record = staff.snapshot()
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent):
            timer = showrunners.timer_for(row, record)
            runners = showrunners.registered_showrunners(known_sessions={}, membership=record)
        self.assertIsInstance(timer, showrunners.TimerRegistration)
        assert isinstance(timer, showrunners.TimerRegistration)
        self.assertEqual(timer.target, f"session:{row.id}")
        self.assertEqual(len(runners), 1)
        self.assertEqual(runners[0]["name"], row.name)
        self.assertEqual(runners[0]["doc"], "")
        self.assertEqual(runners.lookup_failures, ())

    def test_timer_keeps_the_fixed_owner_after_clear(self) -> None:
        row, folder = self.timer("old")
        _ = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("clear")))
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent):
            timer = showrunners.timer_for(row, staff.snapshot())
        self.assertIsInstance(timer, showrunners.TimerRegistration)
        assert isinstance(timer, showrunners.TimerRegistration)
        self.assertEqual(timer.name, row.name)
        sources = showrunners.read_sources(folder.parent, unreadable=showrunners.UnreadableTimerPolicy.STRICT,
                                          record=staff.snapshot())
        self.assertEqual(sources[0].timer.doc, self.root / "old-production.md")

    def test_session_state_adoption_uses_the_staff_id_after_rename_and_clear(self) -> None:
        row, folder = self.timer("old-name")
        _ = staff.rename(row.id, "new-name")
        current = fixture_runtime_id("adoption-cleared-session")
        _ = staff.update_current_session(row.current_session, staff.native_id(current))
        scratch = self.root / "claude-test/project" / current / "scratchpad"
        scratch.mkdir(parents=True)
        write_text_atomic(scratch / "showrunner_state.json", '{"kept": true}\n')
        state = self.root / "state"
        record = staff.snapshot()
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent),
              mock.patch.object(showrunners, "SCRATCH_ROOT", self.root),
              mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state),
              mock.patch.object(staff, "snapshot", side_effect=AssertionError("staff was read again"))):
            found = showrunners.timer_scratch(row.id, record=record)
            self.assertIsInstance(found, showrunners.TimerScratchFound)
            assert isinstance(found, showrunners.TimerScratchFound)
            self.assertEqual(found.path, scratch)
            copied = showrunners.adopt_session_state(row.id, record=record)
        self.assertEqual(copied, ["showrunner_state.json"])
        self.assertEqual((state / row.id / "session/showrunner_state.json").read_text(),
                         (scratch / "showrunner_state.json").read_text())

    def test_unresolved_timer_target_copies_nothing_and_prints_nothing(self) -> None:
        row, folder = self.timer("unresolved")
        write_text_atomic(folder / "conf", "TARGET=session:unknown-owner\n")
        errors = io.StringIO()
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent),
              mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.root / "state"),
              redirect_stderr(errors)):
            result = showrunners.timer_scratch(row.id, record=staff.snapshot())
            self.assertIsInstance(result, showrunners.TimerTargetUnresolved)
            assert isinstance(result, showrunners.TimerTargetUnresolved)
            self.assertEqual(result.target, "session:unknown-owner")
            self.assertEqual(showrunners.adopt_session_state(row.id, record=staff.snapshot()), [])
        self.assertEqual(errors.getvalue(), "")

    def test_absent_scratch_folder_copies_nothing_and_prints_nothing(self) -> None:
        row, folder = self.timer("absent-scratch")
        errors = io.StringIO()
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent),
              mock.patch.object(showrunners, "SCRATCH_ROOT", self.root),
              mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", self.root / "state"),
              redirect_stderr(errors)):
            result = showrunners.timer_scratch(row.id, record=staff.snapshot())
            self.assertIsInstance(result, showrunners.TimerScratchAbsent)
            assert isinstance(result, showrunners.TimerScratchAbsent)
            self.assertEqual(result.session_id, row.current_session)
            self.assertEqual(showrunners.adopt_session_state(row.id, record=staff.snapshot()), [])
        self.assertEqual(errors.getvalue(), "")

    def test_name_keyed_timer_is_not_a_staff_id_registration(self) -> None:
        row, folder = self.timer("hana")
        folder = folder.rename(folder.with_name(f"showrunner-{row.name}"))
        errors = io.StringIO()
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent),
              redirect_stderr(errors)):
            self.assertEqual(showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.STRICT,
                                                     record=staff.snapshot()), [])
        self.assertEqual(errors.getvalue(), "")

    def test_timer_lookup_does_not_scan_name_keyed_targets(self) -> None:
        row, folder = self.timer("hana")
        folder = folder.rename(folder.with_name(f"showrunner-{row.name}"))
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent):
            timer = showrunners.timer_for(row, staff.snapshot())
        self.assertIsInstance(timer, showrunners.NoTimer)

    def test_registered_showrunner_stays_discoverable_without_a_readable_document(self) -> None:
        row, folder = self.timer("hana")
        doc = self.root / "hana-production.md"
        doc.unlink()
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent):
            runners = showrunners.registered_showrunners(known_sessions={}, membership=staff.snapshot())
        self.assertEqual([(runner["name"], runner["doc"]) for runner in runners], [(row.name, str(doc))])
        self.assertEqual(runners.lookup_failures, ())


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

                owner_id = fixture_runtime_id(f"adoption-owner:{name}")

                def interrupted_copy(destination: Path, data: bytes) -> None:
                    if directory:
                        write_bytes_atomic(destination, data)
                    raise InterruptedError("copy stopped")

                with mock.patch.object(showrunners, "write_bytes_atomic", side_effect=interrupted_copy), self.assertRaises(InterruptedError):
                    _ = showrunners.copy_session_state(owner_id, scratch)
                copied = showrunners.copy_session_state(owner_id, scratch)
                self.assertEqual(copied, [name])
                target = showrunners.session_state_dir(owner_id) / name
                if directory:
                    self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()},
                                     {p.name: p.read_bytes() for p in source.iterdir()})
                else:
                    self.assertEqual(target.read_bytes(), source.read_bytes())
                self.assertEqual(sorted(p.name for p in target.parent.iterdir()), [name])
                self.assertEqual(showrunners.copy_session_state(owner_id, scratch), [])

    def test_stalled_session_discovery_obeys_the_callers_deadline(self) -> None:
        reader = stalled_session_reader(self.root)
        self.instance("showrunner-builds", target="session:builds-id", doc=self.doc)
        began = monotonic()
        errors = io.StringIO()
        with mock.patch.object(showrunners, "SESSIONS", reader), redirect_stderr(errors):
            self.assertEqual(showrunners.registered_showrunners(self.notifier, deadline=began + 0.15), [])
        self.assertEqual(errors.getvalue().count("lookup timed out; retained"), 1)
        self.assertLess(monotonic() - began, 1.5)
        self.assertTrue((self.notifier / f"showrunner-{showrunners.owner_for('builds').id}" / "conf").exists())

    def test_failed_lookup_keeps_healthy_rows_and_reserves_inspection_budget(self) -> None:
        self.instance("showrunner-a-stalled", target="session:stalled-id", doc=self.doc)
        self.instance("showrunner-b-healthy", target="session:healthy-id", doc=self.doc)
        for failure in (TimeoutError, OSError):
            clock = [100.0]
            errors = io.StringIO()

            def lookup(target: str, *, deadline: float) -> str:
                if target == "session:" + fixture_runtime_id("stalled-id"):
                    clock[0] = deadline
                    raise failure("fixture lookup failed; retained")
                self.assertGreater(deadline, clock[0])
                return "/fixture/healthy.sock"

            with (self.subTest(failure=failure),
                  mock.patch.object(showrunners, "monotonic", side_effect=lambda: clock[0]),
                  mock.patch.object(showrunners, "socket_for", side_effect=lookup),
                  mock.patch.object(showrunners, "_session_name", return_value="healthy"),
                  redirect_stderr(errors)):
                found = showrunners.registered_showrunners(self.notifier, deadline=210.0)
                self.assertEqual([row["name"] for row in found], ["b-healthy"])
                self.assertEqual(found.exit_code, 1)
                self.assertEqual(found.failures, (errors.getvalue().strip(),))
                self.assertLess(clock[0], 210.0)
                self.assertEqual(errors.getvalue().count("fixture lookup failed; retained"), 1)
                self.assertTrue((self.notifier / f"showrunner-{showrunners.owner_for('a-stalled').id}" / "conf").exists())

    def test_commands_finish_healthy_output_before_reporting_lookup_failure(self) -> None:
        self.showrunner("healthy")
        self.instance("showrunner-stalled", target="session:stalled-id", doc=self.root / "stalled.md")
        original = showrunners.socket_for

        def lookup(target: str) -> str | None:
            if target == "session:" + fixture_runtime_id("stalled-id"):
                raise OSError("fixture registry unreadable")
            return original(target)

        for command in (("list",), ("name", "healthy"), ("address", "healthy"), ("tree",)):
            output = io.StringIO()
            errors = io.StringIO()
            with (self.subTest(command=command),
                  mock.patch.dict(os.environ, self.environment()),
                  mock.patch.multiple(showrunners, NOTIFIER_STATE_DIR=self.notifier,
                                      SESSIONS=self.sessions_script,
                                      SHOWRUNNER_STATE_DIR=self.state),
                  mock.patch.object(showrunners, "socket_for", side_effect=lookup),
                  redirect_stdout(output), redirect_stderr(errors)):
                self.assertEqual(showrunners.main(list(command)), 1)
            self.assertIn("healthy", output.getvalue())
            self.assertEqual(errors.getvalue().count("showrunners: skipping showrunner-stalled:"), 1)
            self.assertNotIn("<not running>", output.getvalue())

    def test_failed_name_and_address_lookups_do_not_claim_the_showrunner_stopped(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        for action in ("name", "address"):
            output, errors = io.StringIO(), io.StringIO()
            with (self.subTest(action=action),
                  mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
                  mock.patch.object(showrunners, "socket_for", side_effect=OSError("fixture registry unreadable")),
                  redirect_stdout(output), redirect_stderr(errors)):
                self.assertEqual(showrunners.main([action, "show"]), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(errors.getvalue().count("fixture registry unreadable"), 1)
            self.assertNotIn("not running", errors.getvalue())

    def test_named_lookup_keeps_unknown_liveness_distinct_from_absence(self) -> None:
        partial = showrunners.ShowrunnerDiscovery([], (
            showrunners.ShowrunnerLookupFailure("missing", "fixture registry unreadable"),))
        with mock.patch.object(showrunners, "registered_showrunners", return_value=partial):
            with self.assertRaisesRegex(OSError, "fixture registry unreadable"):
                _ = showrunners.named("missing")

    def test_single_value_lookups_do_not_turn_failed_discovery_into_absence(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        for lookup in (showrunners.current_name, showrunners.current_address):
            errors = io.StringIO()
            with (self.subTest(lookup=lookup.__name__),
                  mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
                  mock.patch.object(showrunners, "socket_for", side_effect=OSError("fixture registry unreadable")),
                  redirect_stderr(errors)):
                with self.assertRaisesRegex(OSError, "fixture registry unreadable"):
                    _ = lookup("show")
            self.assertEqual(errors.getvalue().count("showrunners: skipping showrunner-show:"), 1)

    def test_lookup_failure_values_keep_caller_reasons_separate_from_log_diagnostics(self) -> None:
        self.instance("showrunner-kid", target="session:kid-session", doc=self.doc)
        self.instance("showrunner-top", target="session:top-session", doc=self.root / "top.md")

        def lookup(target: str) -> str:
            raise OSError(f"cannot tell whether {target} is running: registry unreadable")

        errors = io.StringIO()
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.notifier),
              mock.patch.object(showrunners, "socket_for", side_effect=lookup),
              redirect_stderr(errors)):
            found = showrunners.registered_showrunners()
            for production_name in ("kid", "top"):
                with self.assertRaises(OSError) as caught:
                    _ = showrunners.current_address(production_name)
                self.assertEqual(str(caught.exception),
                                 f"cannot tell whether session:{fixture_runtime_id('kid-session')} is running: registry unreadable; " +
                                 f"cannot tell whether session:{fixture_runtime_id('top-session')} is running: registry unreadable")
                self.assertNotIn("showrunners: skipping", str(caught.exception))
        self.assertEqual([failure.production for failure in found.lookup_failures], ["kid", "top"])
        self.assertEqual([failure.reason for failure in found.lookup_failures], [
            f"cannot tell whether session:{fixture_runtime_id('kid-session')} is running: registry unreadable",
            f"cannot tell whether session:{fixture_runtime_id('top-session')} is running: registry unreadable"])
        self.assertEqual(found.exit_code, 1)
        self.assertEqual(errors.getvalue().splitlines(), list(found.failures) * 3)

    def test_registered_showrunners_uses_supplied_records_without_session_discovery(self) -> None:
        self.instance("showrunner-builds", target="session:builds-id", doc=self.doc)
        self.instance("showrunner-stopped", target="session:stopped-id", doc=self.root / "stopped.md")
        socket_path = Path(self.enterContext(short_socket_directory())) / "builds.sock"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        self.addCleanup(listener.close)
        record: SessionRecord = {
            "pid": os.getpid(), "sessionId": fixture_runtime_id("builds-id"), "name": "builds", "updatedAt": 1,
            "messagingSocketPath": str(socket_path), "tmux": "", "formerNames": [],
        }
        with mock.patch.object(showrunners, "socket_for", side_effect=AssertionError("no sessions subprocess")) as sockets, \
             mock.patch.object(showrunners, "_session_name", side_effect=AssertionError("no session reread")) as names:
            found = showrunners.registered_showrunners(self.notifier, known_sessions={fixture_runtime_id("builds-id"): record})
            owner = showrunners.owner_for("builds")
            _ = staff.rename(owner.id, "renamed-builds")
            record["name"] = "unrelated registry name"
            renamed = showrunners.registered_showrunners(self.notifier, known_sessions={fixture_runtime_id("builds-id"): record})
            absent = showrunners.registered_showrunners(self.notifier, known_sessions={})
        self.assertEqual([(runner["name"], runner["session"], runner["socket"], runner["doc"]) for runner in found], [
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
        conf = self.notifier / f"showrunner-{showrunners.owner_for('z-bad').id}" / "conf"
        conf.unlink()
        conf.mkdir()
        with self.assertRaises(IsADirectoryError) as raised:
            _ = showrunners.registered_showrunners(self.notifier, known_sessions={})
        self.assertIn(str(conf), str(raised.exception))

    def test_list_shows_each_showrunner_under_the_name_it_has_now(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        self.instance("showrunner-other", target="session:stopped-id")
        self.instance("stall-watch", doc=self.doc)
        pid = self.record("first-name", "live-id")
        self.assertEqual(self.successful("list").splitlines(), [
            # A timer whose check names no production doc is not a showrunner's: it is left out.
            f"show\tshow\t{LOS_ANGELES}\t{self.doc}"])
        self.assertEqual(self.successful("name", "show"), "show\n")
        # A unit messages the showrunner's session on this machine, whoever else has its name.
        self.assertEqual(self.successful("address", "show"), f"uds:{self.root / 'live-id.sock'}\n")
        # The staff rename changes each display without changing the native recipient.
        _ = self.record("unrelated registry name", "live-id", pid=pid)
        owner = showrunners.owner_for("show")
        _ = staff.rename(owner.id, "renamed")
        self.assertIn("renamed\trenamed\t", self.successful("list"))
        self.assertEqual(self.successful("name", "renamed"), "renamed\n")
        self.assertEqual(self.successful("address", "renamed"), f"uds:{self.root / 'live-id.sock'}\n")
        self.assertFalse(self.config.exists())

    def test_address_follows_current_session_after_clear_while_timer_keeps_fixed_owner(self) -> None:
        self.showrunner("demo", running=False)
        row = staff.by_path("demo")
        assert row is not None
        current = staff.native_id(fixture_runtime_id("showrunner-after-clear"))
        _ = staff.update_current_session(row.current_session, current)
        _ = self.record("still-demo", current)
        configuration = self.notifier / f"showrunner-{row.id}" / "conf"
        before = configuration.read_bytes(), configuration.stat().st_mtime_ns
        self.assertEqual(self.successful("address", "demo"), f"uds:{self.root / (current + '.sock')}\n")
        self.assertEqual(self.successful("name", "demo"), "demo\n")
        self.assertEqual((configuration.read_bytes(), configuration.stat().st_mtime_ns), before)
        self.assertIn("TARGET=session:" + row.id, configuration.read_text())

    def test_a_stopped_timer_still_names_its_showrunner(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        owner = showrunners.owner_for("show")
        _ = (self.notifier / f"showrunner-{owner.id}" / "state").write_text(
            "ENABLED=0\nNEXT_DUE=0\nLAST_SENT=0\nLAST_RESTART=0\nLAST_TARGET=\n", encoding="utf-8")
        _ = self.record("on-demand", "live-id")
        self.assertEqual(self.successful("name", "show"), "show\n")
        self.assertIn("show\tshow\t", self.successful("list"))
        self.assertEqual([source.timer.name for source in showrunners.read_sources(
            self.notifier, unreadable=showrunners.UnreadableTimerPolicy.STRICT)], ["show"])
        self.assertEqual(showrunners.timer_docs(self.notifier), {"show": self.doc})
        self.assertEqual(showrunners.timer_names(self.notifier), {"show"})
        parent = fixture_member("showrunner", name="parent")
        _ = staff.change_director(owner.id, staff.UnitRole(owner.name, parent.id,
                                 staff.PresentWorktree("natedev", self.root)))
        for staff_state, readers in (("unit", ("registry", "sources")),
                                     ("absent", ("sources", "docs", "names"))):
            if staff_state == "absent":
                self.staff_file.unlink()
            for reader in readers:
                with self.subTest(staff_state=staff_state, reader=reader):
                    errors = io.StringIO()
                    with redirect_stderr(errors):
                        if reader == "registry":
                            result = self.cli("list")
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertEqual(result.stdout, "")
                            self.assertEqual(result.stderr, "")
                        elif reader == "sources":
                            for policy in showrunners.UnreadableTimerPolicy:
                                with self.subTest(policy=policy):
                                    self.assertEqual(showrunners.read_sources(self.notifier, unreadable=policy), [])
                        elif reader == "docs":
                            self.assertEqual(showrunners.timer_docs(self.notifier), {})
                        else:
                            self.assertEqual(showrunners.timer_names(self.notifier), set())
                    self.assertEqual(errors.getvalue(), "")








    def test_name_and_address_refuse_a_showrunner_that_is_not_running_or_has_no_timer(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        for verb in ("name", "address"):
            for production_name in ("show", "absent"):
                with self.subTest(verb=verb, name=production_name):
                    result = self.cli(verb, production_name)
                    self.assertEqual((result.returncode, result.stdout), (1, ""))
                    self.assertIn(f"the showrunner of {production_name} is not running", result.stderr)

    def test_unreadable_session_records_are_an_error_not_a_stopped_showrunner(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        self.assertIn("show\t<not running>", self.successful("list"))
        _ = self.sessions_script.write_text(
            "import sys\nprint('sessions: one or more registry files could not be read', file=sys.stderr)\n"
            + "raise SystemExit(3)\n")
        result = self.cli("list")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("not running", result.stdout)
        self.assertIn(f"cannot tell whether session:{fixture_runtime_id('live-id')} is running: sessions: one or more registry files",
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
            f"{production_name}\t{production_name}\t{LOS_ANGELES}\t{doc}"
            for production_name, doc in (("no-zone", no_zone), ("other-zone", other_zone), ("bad-zone", bad_zone))))
        self.assertNotIn("skipping", result.stderr)

    def test_a_timer_that_is_not_a_complete_showrunners_is_left_out_with_the_reason(self) -> None:
        self.instance("showrunner-no-target", include_target=False)
        self.instance("showrunner-no-doc")
        self.instance("showrunner-unnamed", target="session:unnamed-id", doc=self.root / "unnamed-production.md")
        self.instance("showrunner-lost-doc", doc=self.root / "lost-production.md")
        (self.root / "lost-production.md").unlink()
        self.instance("showrunner-show", doc=self.doc)
        _ = self.record("director", "live-id")
        # The socket establishes liveness; staff supplies the name.
        _ = self.record("gone", "unnamed-id", pid=99999999)
        result = self.cli("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"lost-doc\tlost-doc\t{LOS_ANGELES}\t{self.root / 'lost-production.md'}",
            f"show\tshow\t{LOS_ANGELES}\t{self.doc}",
            f"unnamed\tunnamed\t{LOS_ANGELES}\t{self.root / 'unnamed-production.md'}"])
        self.assertNotIn("skipping showrunner-no-target", result.stderr)
        self.assertNotIn("skipping showrunner-no-doc:", result.stderr)
        self.assertNotIn("no showrunner staff row", result.stderr)
        self.assertNotIn("skipping showrunner-lost-doc", result.stderr)
        self.assertNotIn("skipping showrunner-unnamed", result.stderr)

    def test_the_name_comes_from_the_live_process_not_a_stale_record_of_the_same_session(self) -> None:
        self.instance("showrunner-show", doc=self.doc)
        _ = self.record("stale-name", "live-id", pid=99999999)
        _ = self.record("director", "live-id")
        self.assertEqual(self.successful("name", "show"), "show\n")

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

    def test_settings_read_each_file_change(self) -> None:
        source = Path(__file__).parents[2] / "config/showrunners.conf"
        text = source.read_text()
        _ = self.config.write_text(text)
        self.assertEqual(showrunners.load_settings_from(self.config)["stall_minutes"], 5)
        _ = self.config.write_text(text.replace("stall_minutes=5", "stall_minutes=9"))
        self.assertEqual(showrunners.load_settings_from(self.config)["stall_minutes"], 9)

    def test_settings_read_exact_staff_recipient_ids(self) -> None:
        row = fixture_member("showrunner", name="director")
        _ = self.config.write_text("# range: 0..100\nthreshold_percent=2\n"
                                   + "# range: 0..\nrepeat_minutes=30\n# range: 0..\nstall_minutes=5\n"
                                   + f"faults_to={row.id}\nalways={row.id}\n")
        settings = showrunners.load_settings_from(self.config)
        self.assertEqual(settings["always"], [str(row.id)])
        self.assertEqual(settings["faults_to"], str(row.id))

    def test_an_absent_or_invalid_settings_file_names_the_file(self) -> None:
        for contents in (None, "invalid configuration", "always=wrong"):
            with self.subTest(contents=contents):
                self.config.unlink(missing_ok=True)
                if contents is not None:
                    _ = self.config.write_text(contents)
                with self.assertRaisesRegex(ValueError, re.escape(str(self.config))):
                    _ = showrunners.load_settings_from(self.config)

    def test_delivery_result_tracks_failed_targets_and_preserves_output_lines(self) -> None:
        def address(production_name: str) -> str:
            return "session:" + production_name

        with mock.patch.object(showrunners, "own_name", return_value="self"), \
                mock.patch.object(showrunners, "current_address", side_effect=address), \
                mock.patch.object(showrunners, "send_to_address", wraps=showrunners.send_to_address) as sender, \
                mock.patch.object(subprocess, "run", side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 3, "", "offline")]):
            delivery = showrunners.tell_others({"self": "skip", "top": "hello", "mid": "hello"}, "notice")
        self.assertEqual(list(delivery.lines), ["told top", "could not tell mid: offline"])
        self.assertEqual(delivery.failed, ("mid",))
        self.assertEqual(sender.call_args_list, [mock.call("session:top", "notice", "hello", recipient="top", sender="showrunners"),
                                                 mock.call("session:mid", "notice", "hello", recipient="mid", sender="showrunners")])
        with self.assertRaises(FrozenInstanceError):
            setattr(delivery, "lines", ())
        with self.assertRaises(FrozenInstanceError):
            setattr(delivery, "failed", ())

    def test_public_address_sender_preserves_recipient_sender_key_and_text(self) -> None:
        with mock.patch.dict(os.environ, self.environment()), \
                mock.patch.object(showrunners, "SEND", self.send_script):
            delivery = showrunners.send_to_address("session:unit-id", "transfer-key", "new production\nkeep running",
                                                   recipient="alpha", sender="transfer_unit")
        self.assertEqual(delivery, showrunners.NoticeDelivery(("told alpha",)))
        sent = [json.loads(line) for line in self.sent.read_text().splitlines()]
        self.assertEqual(sent, [["--to", "session:unit-id", "--from", "transfer_unit", "--key", "transfer-key",
                                 "--text", "new production\nkeep running"]])

    def test_commands_refuse_an_unknown_staff_name(self) -> None:
        self.showrunner("new", running=False)
        for verb in ("name", "address"):
            with self.subTest(verb=verb):
                self.assertIn("the showrunner of older is not running", self.refused(verb, "older"))
        for arguments in (("attach", "older", "new"), ("parent", "older")):
            with self.subTest(arguments=arguments):
                self.assertIn("older has no update timer", self.refused(*arguments))

if __name__ == "__main__":
    _ = unittest.main()
