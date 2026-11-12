"""Find staff members through native identities and retain incomplete process evidence."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.lib import unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import fake_unit_host
from scripts.production import showrunners
from scripts.production import unit_lookup
from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production.fake_unit_host import HostedUnit, labeled
from scripts.message.sessions import SessionRecord
from scripts.production.unit_lookup import ClaudeNotRunning, ClaudeUnknown, LiveClaude, UnitState
from scripts.production import staff, session_staff, unit_config
from scripts.agents import remote
from scripts.production.staff_test_support import fixture_member, isolated_staff_file, unit_membership, showrunner_relationship, fixture_runtime_id, fixture_worker, recorded_showrunner, StaffTestCase

SCRIPT = Path(__file__).with_name("unit_lookup.py")




class UnitReferenceTests(StaffTestCase):
    def test_row_production_returns_the_recorded_parent_when_its_document_is_missing(self) -> None:
        doc = self.source_root / "missing-production.md"
        parent = fixture_member("showrunner", name="owner", doc=staff.LiveDoc(remote.local_machine(), doc))
        unit = fixture_member("unit", name="member", parent=parent.id)
        self.assertFalse(doc.exists())
        self.assertEqual(unit_lookup.row_production(unit, staff.snapshot()),
                         unit_lookup.UnitProduction(parent.name, str(doc)))

    def test_selected_identity_keeps_its_parent_after_a_session_and_name_change(self) -> None:
        fixture = self.row_production()
        row = staff.rename(fixture.unit.id, "renamed")
        record = staff.snapshot()
        before = self.staff_file.read_bytes()
        for reference in (session_staff.StoredReference(row.id),
                          session_staff.StoredReference(row.current_session),
                          session_staff.LiveSession(row.current_session)):
            with self.subTest(reference=reference):
                self.assertEqual(unit_lookup.unit_row("wrong-name", record, reference=reference), row)
                self.assertEqual(unit_lookup.unit_parent("wrong-name", reference, record=record),
                                 unit_lookup.UnitProduction(fixture.name, str(fixture.doc)))
        for reference in (session_staff.StoredReference(fixture_runtime_id("missing-reference")),
                          session_staff.LiveSession(fixture_runtime_id("missing-reference"))):
            with self.subTest(reference=reference):
                with self.assertRaises(staff.StaffUnreadable):
                    _ = unit_lookup.unit_row(row.name, record, reference=reference)
                with self.assertRaises(staff.StaffUnreadable):
                    _ = unit_lookup.unit_parent(row.name, reference, record=record)
        self.assertEqual(self.staff_file.read_bytes(), before)


class UnitLookupTests(unittest.TestCase):
    root: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    def test_production_members_excludes_removed_children_and_wrapped_parents_but_keeps_raw_identity(self) -> None:
        live = unit_membership("current", "live")
        retired = unit_membership("current", "retired")
        historical = unit_membership("wrapped", "historical")
        _ = staff.set_worktree(retired.id, staff.RemovedWorktree(remote.local_machine(), self.root / "retired"), expected=retired.worktree)
        assert isinstance(historical.parent, staff.ReportsTo)
        _ = staff.set_doc(historical.parent.id, staff.WrappedDoc(remote.local_machine(), self.root / "archive.md"))
        record = staff.snapshot()
        self.assertEqual(unit_lookup.production_members(record), {"current": (live,), "show": ()})
        self.assertIsNotNone(record.by_id(retired.id))
        self.assertEqual(unit_lookup.unit_row("retired", record, reference=session_staff.StoredReference(retired.id)).id, retired.id)

    def test_retired_session_labels_are_left_out_while_live_siblings_and_retained_identity_stay(self) -> None:
        retired = unit_membership("show", "retired")
        live = unit_membership("show", "live")
        retired = staff.set_worktree(retired.id, staff.RemovedWorktree(remote.local_machine(), self.root / "retired"), expected=retired.worktree)
        record = staff.snapshot()
        sources = unit_lookup.MemberSources(())
        for durable_id in (retired.id, ""):
            with self.subTest(durable_id=bool(durable_id)):
                fake_unit_host.write(self.environment, [
                    labeled("retired", "w1:p1", "retired", CODEX_THREAD_ID=durable_id),
                    labeled("live", "w2:p1", "live", CODEX_THREAD_ID=live.id),
                ])
                found = unit_lookup.labeled_sessions("show", record=record)
                self.assertEqual(list(found), ["live"])
                self.assertEqual(found["live"].place, herdr.HerdrPlace("live", "w2:p1"))
                if durable_id:
                    selected = sources.member(unit_lookup.ObservedUnit(
                        "retired", session_staff.LiveSession(durable_id)), record)
                    self.assertIsInstance(selected, unit_lookup.MemberFound)
                    assert isinstance(selected, unit_lookup.MemberFound)
                    self.assertEqual(selected.row, retired)
                else:
                    with self.assertRaisesRegex(staff.StaffUnreadable, "no native runtime identity"):
                        _ = sources.member(unit_lookup.ObservedUnit("retired"), record)
                self.assertEqual(unit_lookup.unit_row("retired", record, reference=session_staff.StoredReference(retired.id)), retired)

    def test_session_labels_under_a_wrapped_parent_are_left_out_without_hiding_other_units(self) -> None:
        historical = unit_membership("wrapped", "historical")
        live = unit_membership("show", "live")
        assert isinstance(historical.parent, staff.ReportsTo)
        _ = staff.set_doc(historical.parent.id, staff.WrappedDoc(remote.local_machine(), self.root / "wrapped.md"))
        fake_unit_host.write(self.environment, [
            labeled("historical", "w1:p1", "historical", production_name="wrapped", CODEX_THREAD_ID=historical.id),
            labeled("live", "w2:p1", "live", CODEX_THREAD_ID=live.id),
        ])
        found = unit_lookup.every_labeled_session(record=staff.snapshot())
        self.assertEqual({production_name: list(units) for production_name, units in found.units.items()}, {"show": ["live"]})

    def test_active_session_labels_with_missing_or_invalid_parents_still_refuse(self) -> None:
        row = unit_membership("show", "active")
        record = staff.snapshot()
        assert isinstance(row.parent, staff.ReportsTo)
        parent = record.by_id(row.parent.id)
        assert parent is not None
        missing = staff.StaffSnapshot(tuple(item for item in record.rows if item.id != parent.id))
        parentless = staff.StaffSnapshot(tuple(replace(item, parent=staff.StaffRoot()) if item.id == row.id else item
                                              for item in record.rows))
        invalid = staff.StaffSnapshot(tuple(replace(item, kind="worker") if item.id == parent.id else item
                                           for item in record.rows))
        pane = unit_lookup.LabeledSession("active", ClaudeNotRunning(), herdr.HerdrPlace("active", "w1:p1"),
                                      identity=session_staff.LiveSession(row.current_session))
        for broken in (missing, parentless, invalid):
            with self.subTest(rows=broken.rows):
                found = unit_lookup.group_labeled_sessions([pane], record=broken,
                    sources=unit_lookup.MemberSources(()))
                self.assertEqual(found.units, {})

    def test_retained_unit_cleanup_lookup_keeps_a_live_director_after_active_membership_excludes_it(self) -> None:
        row = unit_membership("show", "retired")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("retained-after-clear")))
        self.assertNotEqual(row.id, row.current_session)
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.root / "removed"), expected=row.worktree)
        fake_unit_host.write(self.environment, [labeled("director", "w1:p1", "retired", CODEX_THREAD_ID=row.current_session)])
        self.claude("director", "w1:p1", session_id=row.current_session)
        record = staff.snapshot()
        found = unit_lookup.labeled_session("show", "retired", record=record)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertIsInstance(found.claude, LiveClaude)
        assert isinstance(found.claude, LiveClaude)
        self.assertEqual((found.claude.session_id, found.claude.pid), (row.current_session, os.getpid()))
        self.assertEqual(unit_lookup.labeled_sessions("show", record=record), {})

    def test_retained_unit_cleanup_lookup_keeps_unknown_session_evidence(self) -> None:
        row = unit_membership("show", "retired")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("retained-after-clear")))
        self.assertNotEqual(row.id, row.current_session)
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.root / "removed"), expected=row.worktree)
        fake_unit_host.write(self.environment, [labeled("director", "w1:p1", "retired", CODEX_THREAD_ID=row.current_session)])
        self.claude("director", "w1:p1", session_id=row.current_session)
        _ = (self.records / "broken.json").write_text("{")
        (self.records / "director-1.json").unlink()
        found = unit_lookup.labeled_session("show", "retired", record=staff.snapshot())
        self.assertIsNotNone(found)
        assert found is not None
        self.assertIsInstance(found.claude, ClaudeUnknown)


    def test_removed_same_name_does_not_make_an_active_units_orphan_metadata_ambiguous(self) -> None:
        live = unit_membership("receiving", "alpha")
        old = unit_membership("former", "alpha")
        _ = staff.set_worktree(old.id, staff.RemovedWorktree(remote.local_machine(), self.root / "old"), expected=old.worktree)
        worktree = Path("/live")
        cells = {"Unit": "alpha", "Plan": str(worktree / "plan.md"), "Worktree": "`/live`", "Branch": "alpha"}
        _ = staff.set_worktree(live.id, staff.PresentWorktree(remote.local_machine(), worktree), expected=live.worktree)
        doc = self.root / "receiving-production.md"
        write_text_atomic(doc, "# Receiving production\n")
        owner = showrunner_relationship("receiving")
        _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), doc))
        facts = {"git:/live": ("value", "alpha"),
                 "file:/live/.unit.toml": ("value", 'plan = "plan.md"\nresponsibilities = []\n'),
                 "file:" + str(doc): ("value", doc.read_text())}

        def read_facts(requests: list[tuple[str, str]]) -> dict[str, tuple[str, str]]:
            return {kind + ":" + path: facts[kind + ":" + path] for kind, path in requests}

        config = unit_config.UnitConfig(unit_config.PlanFile(Path("plan.md")), unit_config.NoAppPort(), ())
        with (mock.patch.object(session_staff, "_read_facts", side_effect=read_facts),
              mock.patch.object(unit_config, "read", return_value=config)):
            metadata = unit_lookup.unit_metadata(staff.snapshot())
        self.assertEqual(metadata.cells, {live.id: cells})
        self.assertEqual(metadata.problems, ())

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.environment = {**os.environ,
                            "HOME": str(self.root / "home"),
                            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                            "NOTIFIER_SESSIONS_DIR": str(self.records),
                            "HERDR_PANE_RECORD_DIR": str(self.root / "pane-records"),
                            "UNIT_HISTORY_DIR": str(self.root / "history"),
                            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
                            "HERDR_PROC": str(self.root / "proc"),
                            "HERDR_PLATFORM": "linux",
                            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
                            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json")}
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"stdout": "", "code": 1}}))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        _ = showrunner_relationship("show")
        self.enterContext(mock.patch.dict(os.environ, self.environment))
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "notifier"))

    def hosts(self, units: dict[str, HostedUnit]) -> None:
        fake_unit_host.write(self.environment, list(units.values()))

    def test_transferred_identity_ignores_the_old_production_and_unit_names(self) -> None:
        self.hosts({"unit": labeled("director", "w4:p1", "alpha")})
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        receiving = showrunner_relationship("receiving")
        _ = staff.move(row.id, staff.ReportsTo(receiving.id))
        _ = staff.rename(row.id, "panes")
        self.claude("director", "w4:p1")
        found = unit_lookup.labeled_sessions("receiving")
        self.assertEqual(list(found), ["panes"])
        self.assertEqual(found["panes"].place, herdr.HerdrPlace("director", "w4:p1"))
        self.assertEqual(unit_lookup.labeled_sessions("show"), {})
        observed = [(pane.unit, pane.identity, pane.claude.session_id)
                    for pane in unit_lookup.observed_session_labels()
                    if isinstance(pane.claude, LiveClaude)]
        self.assertEqual(observed, [("", session_staff.LiveSession(row.current_session), row.current_session)])

    def test_legacy_pane_uses_the_unique_unit_name_after_a_transfer(self) -> None:
        row = unit_membership("receiving", "alpha")
        fake_unit_host.write(self.environment, [HostedUnit("director", "w4:p1", {"CODEX_THREAD_ID": row.current_session})])
        self.claude("director", "w4:p1", session_id=row.id)
        self.assertEqual(list(unit_lookup.labeled_sessions("receiving")), [row.name])
        self.assertEqual(unit_lookup.labeled_sessions("former"), {})

    def test_missing_current_row_retains_a_fault_without_a_name_fallback(self) -> None:
        row = unit_membership("show", "alpha")
        native = fixture_runtime_id("late-session-without-row")
        pane = unit_lookup.LabeledSession(row.name, ClaudeUnknown("late hook"),
            herdr.HerdrPlace("late", "w4:p1"),
            session_staff.ObservedProcess(991099, "claude"), session_staff.LiveSession(native))
        before = self.root.joinpath("staff.json").read_bytes()
        found = unit_lookup.group_labeled_sessions([pane])
        self.assertEqual(found.units, {})
        self.assertEqual(self.root.joinpath("staff.json").read_bytes(), before)

    def test_remote_staff_failure_refuses_instead_of_reporting_absence(self) -> None:
        self.hosts({"unit": labeled("director", "w4:p1", "alpha")})
        with mock.patch.object(staff, "snapshot", side_effect=staff.StaffUnreadable("remote host unavailable")):
            with self.assertRaisesRegex(staff.StaffUnreadable, "remote host unavailable"):
                _ = unit_lookup.labeled_session("show", "alpha")

    def test_unreadable_process_environment_refuses_instead_of_reporting_absence(self) -> None:
        row = unit_membership("show", "alpha")
        session = SessionRecord(pid=991099, name=row.name, sessionId=row.current_session,
                                updatedAt=1, messagingSocketPath="/fixture.sock", tmux="", formerNames=[])
        with self.assertRaisesRegex(staff.StaffUnreadable, "process environment is unavailable"):
            _ = session_staff.session_staff_and_directors([session], self.environment, record=staff.snapshot())

    def test_default_identity_and_process_are_named_states(self) -> None:
        pane = unit_lookup.LabeledSession("alpha", ClaudeUnknown("no evidence"), herdr.HerdrPlace("agents", "w1:p1"))
        self.assertIsInstance(pane.identity, session_staff.NoSession)
        self.assertIsInstance(pane.process, session_staff.ProcessUnobserved)

    def test_missing_native_identity_refuses_instead_of_using_an_ambiguous_name(self) -> None:
        _ = unit_membership("show", "alpha")
        _ = unit_membership("receiving", "alpha")
        pane = unit_lookup.LabeledSession("alpha", ClaudeUnknown("identity unavailable"),
                                          herdr.HerdrPlace("director", "w4:p1"))
        found = unit_lookup.group_labeled_sessions([pane])
        self.assertEqual(found.units, {})

    def test_lookup_and_source_import_orders_resolve_the_same_transferred_member(self) -> None:
        row = unit_membership("receiving", "alpha")
        program = """import importlib, sys
for module in sys.argv[1:]:
    importlib.import_module("scripts.production." + module)
from scripts.production import staff_sources, unit_lookup
from scripts.production import staff
record = staff.snapshot()
found = staff_sources.member(staff_sources.UnitMember('old', 'alpha'), record)
assert isinstance(found, staff_sources.NoSuchMember)

marked = unit_lookup.LabeledSession('alpha', unit_lookup.ClaudeNotRunning(),
                                unit_lookup.HerdrPlace('agents', 'w1:p1'),
                                identity=unit_lookup.LiveSession(record.find(kind='unit')[0].current_session))
grouped = unit_lookup.group_labeled_sessions([marked], record=record)
assert list(grouped.units['receiving']) == ['alpha']
observed = staff_sources.member(unit_lookup.ObservedUnit('alpha', marked.identity), record)
assert isinstance(observed, staff_sources.MemberFound)
print(observed.row.id)
"""
        for order in (("unit_lookup", "staff_sources"), ("staff_sources", "unit_lookup")):
            with self.subTest(order=order):
                result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", program, *order],
                                        env=self.environment, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, row.id + "\n")

    def test_mac_membership_discovery_reads_the_owner_once_after_a_transfer(self) -> None:
        transport = StaffTestCase()
        transport.setUp()
        self.addCleanup(transport.doCleanups)
        self.hosts({"unit": labeled("director", "w4:p1", "alpha")})
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        receiving = showrunner_relationship("receiving")
        _ = staff.move(row.id, staff.ReportsTo(receiving.id))
        self.claude("director", "w4:p1")
        with transport.machine("mac"), mock.patch.dict(os.environ, {
            "NOTIFIER_SESSIONS_DIR": str(self.records),
            "XDG_CONFIG_HOME": self.environment.get("XDG_CONFIG_HOME", str(Path(self.environment["HOME"]) / ".config")),
        }):
            found = unit_lookup.every_labeled_session()
        self.assertEqual(list(found.units["receiving"]), ["alpha"])
        self.assertEqual(found.units["receiving"]["alpha"].place,
                         herdr.HerdrPlace("director", "w4:p1"))
        self.assertEqual(len(transport.ssh_commands()), 1)

    def test_session_marks_use_the_supplied_snapshot_after_a_transfer(self) -> None:
        self.hosts({"unit": labeled("director", "w4:p1", "alpha")})
        self.claude("director", "w4:p1")
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        receiving = showrunner_relationship("receiving")
        _ = staff.move(row.id, staff.ReportsTo(receiving.id))
        record = staff.snapshot()
        session = sessions.read_session(self.records / "director-1.json")
        assert not isinstance(session, sessions.UnreadableSessionRecord)
        with (mock.patch.object(staff, "snapshot", side_effect=AssertionError("second owner read")),
              mock.patch.object(herdr, "herdr_processes", side_effect=AssertionError("process scan"))):
            found, directors = unit_lookup.session_staff_and_directors([session], self.environment, record=record)
        self.assertEqual(list(found.units["receiving"]), ["alpha"])
        self.assertEqual(directors, {})

    def test_staff_identity_alone_joins_a_pane_without_legacy_staff_marks(self) -> None:
        row = unit_membership("receiving", "alpha")
        fake_unit_host.write(self.environment, [HostedUnit("director", "w4:p1", {"CODEX_THREAD_ID": row.id})])
        self.claude("director", "w4:p1")
        found = unit_lookup.labeled_sessions("receiving")
        self.assertEqual(list(found), ["alpha"])
        self.assertIsInstance(found["alpha"].claude, LiveClaude)

    def test_restarted_unit_without_process_marks_uses_its_repaired_pane_record(self) -> None:
        from scripts.agents import pane_record
        from scripts.production import session_staff
        from scripts.production import session_pane

        owner = unit_membership("show", "alpha")
        fake_unit_host.write(self.environment, [HostedUnit("agents", "w4:p1", {})])
        self.claude("alpha", "w4:p1", session_id=owner.id)
        pid = os.getpid()
        registry = self.records / f"{pid}.json"
        _ = registry.write_bytes((self.records / "alpha-1.json").read_bytes())
        record = sessions.read_session(registry)
        assert not isinstance(record, sessions.UnreadableSessionRecord)
        marks = {"CODEX_THREAD_ID": owner.id}
        pane_record.write("agents", pane_record.launch("w4:p1", owner.id, str(self.root), marks, ["claude"]))
        found = unit_lookup.labeled_sessions("show")
        self.assertIsInstance(found["alpha"].claude, LiveClaude)
        self.assertEqual(found["alpha"].identity, session_staff.LiveSession(owner.current_session))
        self.assertEqual(session_pane.record_place(record), herdr.HerdrPlace("agents", "w4:p1"))
        self.assertEqual(session_pane.session_place(owner.id), herdr.HerdrPlace("agents", "w4:p1"))
        grouped, _ = session_staff.session_staff_and_directors([record], self.environment)
        self.assertIsInstance(grouped.units["show"]["alpha"].claude, LiveClaude)

    def test_live_staffed_session_without_any_launch_agrees_across_readers_and_worker_queue(self) -> None:
        from scripts.production import session_staff
        from scripts.message import unit_labels
        from scripts.production import session_pane

        doc = self.root / "production.md"
        _ = doc.write_text("# show\n")
        _ = recorded_showrunner("show", doc)
        owner = unit_membership("show", "alpha")
        fake_unit_host.write(self.environment, [HostedUnit("agents", "w4:p1", {})])
        self.claude("alpha", "w4:p1", session_id=owner.id)
        registry = self.records / f"{os.getpid()}.json"
        _ = registry.write_bytes((self.records / "alpha-1.json").read_bytes())
        session = sessions.read_session(registry)
        assert not isinstance(session, sessions.UnreadableSessionRecord)
        self.assertFalse((self.root / "pane-records/agents.json").exists())
        found = unit_lookup.labeled_sessions("show")
        self.assertIsInstance(found["alpha"].claude, LiveClaude)
        self.assertEqual(found["alpha"].identity, session_staff.LiveSession(owner.current_session))
        self.assertEqual(session_pane.session_place(owner.id), herdr.HerdrPlace("agents", "w4:p1"))
        grouped, _ = session_staff.session_staff_and_directors([session], self.environment)
        self.assertEqual(grouped.units["show"]["alpha"].identity, session_staff.LiveSession(owner.current_session))
        run_root = self.root / "runs"
        run = run_root / "older-run"
        run.mkdir(parents=True)
        _ = (run / "director").write_text(owner.id)
        worker = fixture_worker("fix", owner.id, runtime_id=fixture_runtime_id("worker-thread"), backend=staff.CodexBackend(self.root / "codex-home"))
        registered = [{"name": "show", "session": "runner", "socket": "", "zone": "UTC", "doc": "/unused"}]
        with (mock.patch.object(unit_labels, "newest_sessions", return_value=[session]),
              mock.patch.object(unit_labels, "registered_showrunners", return_value=registered),
              mock.patch.object(herdr, "herdr_processes", side_effect=AssertionError("queue must read files only"))):
            labels = unit_labels.unit_labels_for([owner.id, str(worker.current_session)],
                {**self.environment, "ROSTER_RUN_ROOT": str(run_root)})
        self.assertEqual(labels[owner.id], unit_labels.UnitLabels("alpha", "show", "alpha"))
        self.assertEqual(labels[str(worker.current_session)], unit_labels.UnitLabels("fix", "show", "alpha"))

    def test_repair_creates_the_missing_live_launch_from_staff_and_rerun_writes_nothing(self) -> None:
        from scripts.production import session_staff
        from scripts.agents import pane_record

        owner = unit_membership("show", "alpha")
        fake_unit_host.write(self.environment, [HostedUnit("agents", "w4:p1", {})])
        self.claude("alpha", "w4:p1", session_id=owner.id)
        _ = (self.records / f"{os.getpid()}.json").write_bytes((self.records / "alpha-1.json").read_bytes())
        with mock.patch.object(pane_record, "PROC", self.root / "proc"):
            changes = session_staff.repair_marks()
            self.assertEqual(len(changes), 1)
            entry = pane_record.read("agents")["w4:p1"]
            self.assertEqual(entry["session_id"], owner.id)
            self.assertEqual(entry["environment"], {})
            path = pane_record.record_path("agents")
            before = path.read_bytes(), path.stat().st_mtime_ns
            self.assertEqual(session_staff.repair_marks(), [])
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_folder_labels_reach_tab_sort_queue_and_repair_with_an_unrelated_session_name(self) -> None:
        from scripts.production import session_staff
        from scripts.message import unit_labels
        from scripts.agents import pane_record

        doc = self.root / "production.md"
        _ = doc.write_text("# show\n")
        _ = recorded_showrunner("show", doc)
        owner = unit_membership("show", "alpha")
        _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), self.root), expected=owner.worktree)
        fake_unit_host.write(self.environment, [HostedUnit("agents", "w4:p1", {})])
        self.claude("organon", "w4:p1", session_id=owner.id)
        raw = cast(dict[str, object], json.loads((self.records / "organon-1.json").read_text()))
        raw["cwd"] = str(self.root)
        _ = (self.records / f"{os.getpid()}.json").write_text(json.dumps(raw))
        session = sessions.read_session(self.records / f"{os.getpid()}.json")
        assert not isinstance(session, sessions.UnreadableSessionRecord)
        self.assertFalse((self.root / "pane-records/agents.json").exists())

        # Tab sorting consumes the environments resolved by herdr_processes.
        processes = herdr.herdr_processes()
        classifier = herdr.UnitTabClassifier("agents", processes, staff.snapshot())
        self.assertIsInstance(classifier.classify("show", "organon", ["w4:p1"]), herdr.UnitTab)
        self.assertEqual(herdr.tab_order("show", ["show", "aaa-other", "organon"], [], {"organon"}), [0, 2, 1])
        grouped, _ = session_staff.session_staff_and_directors([session], self.environment)
        self.assertEqual(grouped.units["show"]["alpha"].identity, session_staff.LiveSession(owner.current_session))
        registered = [{"name": "show", "session": "runner", "socket": "", "zone": "UTC", "doc": "/unused"}]
        with (mock.patch.object(unit_labels, "newest_sessions", return_value=[session]),
              mock.patch.object(unit_labels, "registered_showrunners", return_value=registered),
              mock.patch.object(herdr, "herdr_processes", side_effect=AssertionError("queue must read files only"))):
            labels = unit_labels.unit_labels_for([owner.id], self.environment)
        self.assertEqual(labels[owner.id], unit_labels.UnitLabels("alpha", "show", "alpha"))

        with mock.patch.object(pane_record, "PROC", self.root / "proc"):
            changes = session_staff.repair_marks()
            self.assertEqual(len(changes), 1)
            entry = pane_record.read("agents")["w4:p1"]
            self.assertEqual(entry["environment"], {})
            path = pane_record.record_path("agents")
            before = path.read_bytes(), path.stat().st_mtime_ns
            self.assertEqual(session_staff.repair_marks(), [])
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def renamed_production(self) -> None:
        for name in ("trunk", "other", "first", "second", "third"):
            _ = unit_membership("new", name)
        doc = self.root / "new-production.md"
        _ = doc.write_text("# Production\n")
        owner = recorded_showrunner("new", doc)
        state = self.root / "showrunner-state"
        _ = self.enterContext(mock.patch.object(showrunners, "SHOWRUNNER_STATE_DIR", state))
        self.environment["SHOWRUNNER_STATE_DIR"] = str(state)
        instance = self.root / f"notifier/showrunner-{showrunner_relationship('new').id}"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{owner.id}\nCHECK=zsh /scripts/production_check.sh {owner.id}\n")

    def process(self, pid: int, parent: int, argv: list[str], **environment: str) -> None:
        fake_unit_host.process(self.environment, pid, parent, argv, environment, cwd=self.root)

    def herdr_session(self, name: str = "native", pane: str = "w1:p1", unit: str = "trunk") -> None:
        row = unit_membership("new" if (self.root / "new-production.md").exists() else "show", unit)
        fake_unit_host.write(self.environment, [labeled(name, pane, unit,
            production_name="new" if (self.root / "new-production.md").exists() else "show",
            CODEX_THREAD_ID=row.current_session)])
        self.process(os.getpid(), 991001, ["claude"], HERDR_PANE_ID=pane,
                     CODEX_THREAD_ID=row.current_session)
        shell_environment = self.root / "proc/991001/environ"
        values = [value for value in shell_environment.read_text().split("\0")
                  if not value.startswith(("CODEX_THREAD_ID=", "CLAUDE_CODE_SESSION_ID="))]
        _ = shell_environment.write_text("\0".join(values))

    def claude(self, name: str, pane: str, *, updated: int = 1, session_id: str = "", pid: int | None = None) -> None:
        """Record a live Claude in `pane`: this test process stands in for it."""
        pid = os.getpid() if pid is None else pid
        existing = self.root / "proc" / str(pid) / "environ"
        current = dict(item.split("=", 1) for item in existing.read_text().split("\0") if "=" in item) if existing.exists() else {}
        if pane and current.get("HERDR_PANE_ID") != pane:
            fake_unit_host.place_process(self.environment, pid, pane, exec_shell=True)
        raw = (self.root / "proc" / str(pid) / "environ").read_bytes().decode().split("\0")
        marks = dict(item.split("=", 1) for item in raw if "=" in item)
        identity = session_id or marks.get("CODEX_THREAD_ID") or fixture_runtime_id("lookup-backend:" + name)
        path = Path(self.enterContext(short_socket_directory())) / f"{name}-{updated}.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        _ = (self.records / f"{name}-{updated}.json").write_text(json.dumps({
            "pid": pid, "sessionId": identity, "name": name,
            "messagingSocketPath": str(path), "updatedAt": updated, "tmux": f"label-at-start:@1.{pane}" if pane else ""}))

    def live_hosts(self, *panes: str) -> None:
        for pane in panes:
            self.claude(pane, pane, pid=fake_unit_host.live_pid(self))

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = self.environment
        return subprocess.run([str(SCRIPT.parents[1] / "lib/py"), str(SCRIPT), *arguments], env=environment, capture_output=True,
                              text=True, check=False)


    def test_a_unit_is_found_by_its_mark_whatever_its_session_is_called_now(self) -> None:
        self.hosts({"$1": labeled("renamed-since-launch", "w4:p1", "trunk"),
                                     "$2": labeled("another-show", "w5:p1", "trunk", production_name="other"),
                                     "$3": HostedUnit("unmarked", "w6:p1", {})})
        self.claude("renamed-since-launch", "w4:p1", session_id=unit_membership("show", "trunk").id)
        found = unit_lookup.labeled_sessions("show")
        self.assertEqual(list(found), ["trunk"])
        unit = found["trunk"]
        self.assertEqual((unit.place, unit.place.session), (herdr.HerdrPlace("renamed-since-launch", "w4:p1"), "renamed-since-launch"))
        assert isinstance(unit.claude, LiveClaude)
        self.assertEqual((unit.claude.name, unit.claude.session_id), ("renamed-since-launch", unit_membership("show", "trunk").id))

    def test_session_names_do_not_change_the_production_name(self) -> None:
        self.renamed_production()
        self.hosts({"$1": labeled("still-old", "w4:p1", "trunk", production_name="new")})
        self.claude("still-old", "w4:p1")
        found = unit_lookup.every_labeled_session()
        self.assertEqual(list(found.units), ["new"])
        for production_name in ("new",):
            with self.subTest(name=production_name):
                self.assertEqual(unit_lookup.labeled_sessions(production_name)["trunk"].place, herdr.HerdrPlace("still-old", "w4:p1"))
                pane = self.run_script("pane", production_name, "trunk")
                self.assertEqual((pane.returncode, pane.stdout), (0, "herdr:still-old:w4:p1\n"), pane.stderr)
                listed = self.run_script("list", production_name)
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertEqual(listed.stdout.split("\t")[:6],
                                 ["trunk", "herdr:still-old:w4:p1", "still-old", "live", "still-old", unit_membership("new", "trunk").id])

    def test_grouping_uses_staff_names_without_discovering_processes(self) -> None:
        self.renamed_production()
        unit = unit_lookup.LabeledSession("trunk", LiveClaude("report", "report-id", "/fixture.sock", 123),
                                      herdr.HerdrPlace("old-pane", "w4:p1"), identity=session_staff.LiveSession(unit_membership("new", "trunk").current_session))
        with mock.patch.object(herdr, "herdr_processes", side_effect=AssertionError("no process scan")) as scan:
            found = unit_lookup.group_labeled_sessions(iter([unit]))
        self.assertEqual(found.units, {"new": {"trunk": unit}})
        scan.assert_not_called()

    def test_grouping_uses_the_current_row_name_after_a_rename(self) -> None:
        row = unit_membership("show", "old-name")
        row = staff.rename(row.id, "renamed")
        pane = unit_lookup.LabeledSession("old-name", ClaudeUnknown("unreadable record"),
            herdr.HerdrPlace("agents", "w1:p1"), session_staff.ObservedProcess(991099, "claude"),
            session_staff.LiveSession(row.current_session))
        record = staff.snapshot()
        with mock.patch.object(herdr, "herdr_processes", side_effect=AssertionError("no process scan")) as scan:
            found = unit_lookup.group_labeled_sessions((pane,), record=record)
        self.assertEqual(found.units, {"show": {row.name: replace(pane, unit=row.name)}})
        self.assertEqual(found.members, {"show": (row.name,)})
        scan.assert_not_called()






    def test_native_sessions_do_not_read_timers_during_discovery(self) -> None:
        self.renamed_production()
        self.hosts({
            "$1": labeled("first", "w4:p1", "first", production_name="new"),
            "$2": labeled("second", "w5:p1", "second", production_name="new"),
            "$3": labeled("third", "w6:p1", "third", production_name="new"),
        })
        self.live_hosts("w4:p1", "w5:p1", "w6:p1")
        sources = unit_lookup.member_sources()
        with mock.patch.object(showrunners, "_source", wraps=getattr(showrunners, "_source")) as reads:
            for _ in range(2):
                found = unit_lookup.every_labeled_session(sources=sources)
                self.assertEqual(set(found.units["new"]), {"first", "second", "third"})
                self.assertEqual(reads.call_count, 0)

    def test_current_production_discovery_keeps_an_existing_timer_read_budget(self) -> None:
        self.renamed_production()
        self.hosts({"$1": labeled("current", "w4:p1", "trunk", production_name="new")})
        self.claude("current", "w4:p1")
        sources = unit_lookup.member_sources()
        with mock.patch.object(showrunners, "_source", wraps=getattr(showrunners, "_source")) as reads:
            source = showrunners.read_sources(self.root / "notifier",
                unreadable=showrunners.UnreadableTimerPolicy.STRICT, record=staff.snapshot())[0]
            self.assertIsInstance(source, showrunners.ShowrunnerSource)
            found = unit_lookup.every_labeled_session(sources=sources)
            self.assertEqual(list(found.units["new"]), ["trunk"])
            self.assertEqual(reads.call_count, 1, "current marks must not reread already inventoried timers")

    def test_current_production_lookup_does_not_read_timer_docs(self) -> None:
        self.renamed_production()
        self.hosts({"$1": labeled("current", "w4:p1", "trunk", production_name="new")})
        self.claude("current", "w4:p1")
        sources = unit_lookup.member_sources()
        with mock.patch.object(showrunners, "_source", wraps=getattr(showrunners, "_source")) as reads:
            found = unit_lookup.labeled_sessions("new", sources=sources)
            self.assertEqual(found["trunk"].place, herdr.HerdrPlace("current", "w4:p1"))
            self.assertEqual(reads.call_count, 0)

    def test_herdr_marks_are_grouped_under_the_current_production(self) -> None:
        self.renamed_production()
        self.herdr_session()
        self.claude("current", "")
        environment = self.root / "proc" / str(os.getpid()) / "environ"
        _ = environment.write_bytes(b"HERDR_PANE_ID=w1:p1")
        self.assertEqual(list(unit_lookup.every_labeled_session().units), ["new"])
        for production_name in ("new",):
            with self.subTest(name=production_name):
                pane = self.run_script("pane", production_name, "trunk")
                self.assertEqual((pane.returncode, pane.stdout), (0, "herdr:native:w1:p1\n"), pane.stderr)


    def test_a_relaunch_in_the_same_pane_is_found_from_its_newest_record(self) -> None:
        self.hosts({"$1": labeled("trunk", "w4:p1", "trunk")})
        old_id = unit_membership("show", "trunk").id
        new_id = fixture_runtime_id("lookup-relaunched-backend")
        self.claude("before", "w4:p1", updated=1, session_id=old_id)
        _ = staff.update_current_session(old_id, staff.native_id(new_id))
        self.claude("after", "w4:p1", updated=2, session_id=new_id)
        claude = unit_lookup.labeled_sessions("show")["trunk"].claude
        assert isinstance(claude, LiveClaude)
        self.assertEqual((claude.name, claude.session_id), ("after", new_id))
        row = staff.by_id(old_id)
        assert row is not None
        self.assertEqual(row.current_session, new_id)
        self.assertEqual(staff.by_current_session(new_id), row)
        self.assertIsNone(staff.by_current_session(old_id))

    def test_a_labeled_session_with_no_claude_is_still_the_units_pane(self) -> None:
        self.hosts({"$1": labeled("trunk", "w4:p1", "trunk")})
        unit = unit_lookup.labeled_sessions("show")["trunk"]
        self.assertEqual(unit.place, herdr.HerdrPlace("trunk", "w4:p1"))
        self.assertIsInstance(unit.claude, ClaudeUnknown)



    def test_an_unreadable_record_leaves_a_unit_without_a_match_unknown_not_stopped(self) -> None:
        self.hosts({"$1": labeled("trunk", "w4:p1", "trunk"), "$2": labeled("fps", "w5:p1", "fps")})
        self.claude("fps", "w5:p1")
        _ = (self.records / "broken.json").write_text("{")
        found = unit_lookup.labeled_sessions("show")
        self.assertIsInstance(found["trunk"].claude, ClaudeUnknown)
        self.assertIsInstance(found["fps"].claude, LiveClaude)

    def test_a_broken_record_of_an_exited_session_leaves_every_unit_stopped(self) -> None:
        # During an account move many sessions exit at once; their records are not evidence about other panes.
        self.hosts({"$1": labeled("trunk", "w4:p1", "trunk")})
        exited = subprocess.Popen(["true"])
        _ = exited.wait(timeout=TEST_HANG_GUARD_SECS)
        _ = (self.records / f"{exited.pid}.json").write_text("{")
        self.assertIsInstance(unit_lookup.labeled_sessions("show")["trunk"].claude, ClaudeUnknown)

    def test_a_broken_record_of_a_live_session_makes_only_its_own_pane_unknown(self) -> None:
        self.hosts({"$1": labeled("trunk", "w4:p1", "trunk"), "$2": labeled("fps", "w5:p1", "fps")})
        pid = fake_unit_host.live_pid(self)
        fake_unit_host.place_process(self.environment, pid, "w5:p1", exec_shell=True)
        _ = (self.records / f"{pid}.json").write_text("{")
        found = unit_lookup.labeled_sessions("show")
        self.assertIsInstance(found["trunk"].claude, ClaudeUnknown)
        self.assertEqual(found["fps"].claude, ClaudeUnknown(f"the session record of pid {pid} could not be read"))





    def test_the_command_prints_the_pane_and_tells_absent_from_cannot_tell(self) -> None:
        self.hosts({"$1": labeled("trunk", "w4:p1", "trunk")})
        self.claude("trunk", "w4:p1")
        found = self.run_script("pane", "show", "trunk")
        self.assertEqual((found.returncode, found.stdout), (0, "herdr:trunk:w4:p1\n"))
        self.assertEqual(self.run_script("pane", "show", "fps").returncode, 1)
        listed = self.run_script("list", "show")
        self.assertEqual(listed.stdout.split("\t")[:4], ["trunk", "herdr:trunk:w4:p1", "trunk", "live"])
        _ = (self.root / "herdr-responses.json").write_text("not json")
        self.assertEqual(self.run_script("pane", "show", "trunk").returncode, 3)
        self.assertEqual(self.run_script("list").returncode, 2)


    def test_a_herdr_unit_joins_its_newest_live_claude_and_deduplicates_processes(self) -> None:
        self.herdr_session()
        self.process(991001, 991000, ["zsh"], HERDR_PANE_ID="w1:p1",
                     CODEX_THREAD_ID=unit_membership("show", "trunk").current_session)
        self.claude("old", "", updated=1)
        self.claude("current", "", updated=2)
        found = unit_lookup.labeled_sessions("show")
        self.assertEqual(list(found), ["trunk"])
        unit = found["trunk"]
        self.assertEqual((unit.place.session, unit.place), ("native", herdr.HerdrPlace("native", "w1:p1")))
        assert isinstance(unit.claude, LiveClaude)
        self.assertEqual(unit.claude.name, "current")
        self.assertEqual(self.run_script("pane", "show", "trunk").stdout, "herdr:native:w1:p1\n")
        self.assertEqual(self.run_script("list", "show").stdout.split("\t")[:4],
                         ["trunk", "herdr:native:w1:p1", "native", "live"])

    def test_attach_started_server_joins_live_claude_by_its_listening_socket(self) -> None:
        self.herdr_session(name="writing")
        server = self.root / "proc/991000"
        _ = (server / "cmdline").write_bytes(b"herdr\0server\0")
        _ = (server / "environ").write_bytes(b"HERDR_SESSION=writing\0")
        self.claude("writer", "", session_id=unit_membership("show", "trunk").id)
        found = unit_lookup.labeled_sessions("show")["trunk"]
        self.assertEqual(found.place, herdr.HerdrPlace("writing", "w1:p1"))
        self.assertIsInstance(found.claude, LiveClaude)
        assert isinstance(found.claude, LiveClaude)
        self.assertEqual((found.claude.name, found.claude.session_id), ("writer", unit_membership("show", "trunk").id))
        self.assertEqual(self.run_script("list", "show").stdout.split("\t")[:4],
                         ["trunk", "herdr:writing:w1:p1", "writing", "live"])

    def test_only_proved_ancestor_launchers_leave_the_process_evidence(self) -> None:
        row = unit_membership("show", "trunk")
        place = herdr.HerdrPlace("native", "w1:p1")
        running = [SessionRecord(pid=991099, name="director", sessionId=row.current_session,
                                 updatedAt=1, messagingSocketPath="/fixture.sock", tmux="", formerNames=[])]
        for command, parent, unavailable, retained in (
            ("zsh", 991001, False, False),
            ("python3", 991001, False, False),
        ):
            with self.subTest(command=command, parent=parent, unavailable=unavailable):
                self.process(991001, 991000, [command], CODEX_THREAD_ID=row.current_session)
                self.process(991099, parent, ["claude"], CODEX_THREAD_ID=row.current_session)
                processes = [herdr.HerdrProcess(991001, place, {"CODEX_THREAD_ID": row.current_session}, command, ""),
                             herdr.HerdrProcess(991099, place, {}, "claude", "")]
                with (mock.patch.object(herdr, "herdr_processes", return_value=processes),
                      mock.patch.object(herdr, "live_sessions", return_value=[{"name": "native"}]),
                      mock.patch.object(unit_lookup, "live_session", return_value=True)):
                    if unavailable:
                        with mock.patch.object(herdr, "linux_process_parent", side_effect=OSError("parent unavailable")):
                            observed = unit_lookup.observed_session_labels(record=staff.snapshot(), running=running)
                    else:
                        observed = unit_lookup.observed_session_labels(record=staff.snapshot(), running=running)
                self.assertEqual([pane.process.pid for pane in observed
                                  if isinstance(pane.process, session_staff.ObservedProcess)],
                                 [991001, 991099] if retained else [991099])
                grouped = unit_lookup.group_labeled_sessions(observed)
                self.assertIsInstance(grouped.units["show"]["trunk"].claude, LiveClaude)



    def test_registered_claude_with_a_headless_claude_child_of_the_same_id_stays_live(self) -> None:
        row = unit_membership("show", "trunk")
        place = herdr.HerdrPlace("native", "w1:p1")
        running = [SessionRecord(pid=991099, name="director", sessionId=row.current_session,
                                 updatedAt=1, messagingSocketPath="/fixture.sock", tmux="", formerNames=[])]
        self.process(991099, 991000, ["claude"], CLAUDE_CODE_SESSION_ID=row.current_session)
        self.process(991200, 991099, ["claude", "--print"], CLAUDE_CODE_SESSION_ID=row.current_session)
        processes = [herdr.HerdrProcess(991099, place, {}, "claude", ""),
                     herdr.HerdrProcess(991200, place, {"CLAUDE_CODE_SESSION_ID": row.current_session}, "claude", "")]
        with (mock.patch.object(herdr, "herdr_processes", return_value=processes),
              mock.patch.object(herdr, "live_sessions", return_value=[{"name": "native"}]),
              mock.patch.object(unit_lookup, "live_session", return_value=True)):
            observed = unit_lookup.observed_session_labels(record=staff.snapshot(), running=running)
        self.assertEqual([pane.process.pid for pane in observed
                          if isinstance(pane.process, session_staff.ObservedProcess)], [991099])
        grouped = unit_lookup.group_labeled_sessions(observed)
        self.assertIsInstance(grouped.units["show"]["trunk"].claude, LiveClaude)

    def test_stopped_shell_does_not_read_the_darwin_process_snapshot(self) -> None:
        from scripts.message import process_table

        row = unit_membership("show", "trunk")
        self.hosts({"stopped": labeled("native", "w1:p1", "trunk", CODEX_THREAD_ID="",
                                      CLAUDE_CODE_SESSION_ID=row.current_session)})
        with herdr.process_snapshot():
            _ = herdr.herdr_processes()
            with (mock.patch.dict(os.environ, {"HERDR_PLATFORM": "darwin"}),
                  mock.patch.object(process_table, "snapshot", wraps=process_table.snapshot) as snapshot):
                found = unit_lookup.every_labeled_session()
        self.assertIsInstance(found.status_units("show")[row.name].claude, ClaudeNotRunning)
        self.assertEqual(snapshot.call_count, 0)

    def test_live_claude_with_deleted_cwd_joins_its_marked_shell(self) -> None:
        self.herdr_session(name="writing")
        self.process(991001, 991000, ["zsh"], HERDR_PANE_ID="w1:p1",
                     CODEX_THREAD_ID=unit_membership("show", "trunk").current_session)
        claude_cwd = self.root / "proc" / str(os.getpid()) / "cwd"
        claude_cwd.unlink()
        claude_cwd.symlink_to(self.root / "deleted-directory")
        self.claude("writer", "", session_id=unit_membership("show", "trunk").id)
        found = unit_lookup.labeled_sessions("show")["trunk"]
        self.assertEqual(found.place, herdr.HerdrPlace("writing", "w1:p1"))
        self.assertIsInstance(found.claude, LiveClaude)
        assert isinstance(found.claude, LiveClaude)
        self.assertEqual((found.claude.pid, found.claude.session_id), (os.getpid(), unit_membership("show", "trunk").id))
        self.assertEqual(self.run_script("list", "show").stdout.split("\t")[:4],
                         ["trunk", "herdr:writing:w1:p1", "writing", "live"])

    def test_a_herdr_unit_without_a_live_record_is_stopped_or_unknown(self) -> None:
        self.hosts({"native": labeled("native", "w1:p1", "trunk")})
        self.assertIsInstance(unit_lookup.labeled_sessions("show")["trunk"].claude, ClaudeUnknown)
        environment = self.root / "proc/991001/environ"
        _ = environment.write_bytes(environment.read_bytes().replace(b"CODEX_THREAD_ID=", b"CLAUDE_CODE_SESSION_ID="))
        self.assertIsInstance(unit_lookup.labeled_sessions("show")["trunk"].claude, ClaudeNotRunning)
        _ = (self.records / "broken.json").write_text("{")
        self.assertIsInstance(unit_lookup.labeled_sessions("show")["trunk"].claude, ClaudeUnknown)


    def test_discovery_needs_only_herdr_when_path_has_no_session_tools(self) -> None:
        self.herdr_session()
        self.claude("current", "")
        run = subprocess.run
        with mock.patch("shutil.which", return_value=None), mock.patch.object(subprocess, "run", wraps=run) as commands:
            found = unit_lookup.every_labeled_session()
        self.assertEqual(list(found.units["show"]), ["trunk"])
        self.assertTrue(commands.call_args_list)
        self.assertTrue(all(call.args[0][0] == self.environment["HERDR_BIN"] for call in commands.call_args_list))

    def test_unreadable_herdr_refuses_discovery_and_an_empty_answer_is_complete(self) -> None:
        with self.assertRaises(OSError):
            _ = unit_lookup.every_labeled_session()
        self.hosts({})
        self.assertEqual(unit_lookup.every_labeled_session(), unit_lookup.LabeledProductions({}))

    def test_a_detached_run_carrying_the_marks_does_not_double_the_units_pane(self) -> None:
        # A worker build that detached_runs supervises keeps the unit's marks and an old pane id.
        self.hosts({"$1": labeled("agents", "w2:pS", "trunk"), "$2": labeled("runs", "w2:pE", "trunk", CODEX_THREAD_ID="")})
        self.claude("trunk", "w2:pS")
        found = unit_lookup.every_labeled_session()
        unit = found.units["show"]["trunk"]
        self.assertEqual(unit.place, herdr.HerdrPlace("agents", "w2:pS"))
        self.assertIsInstance(unit.claude, LiveClaude)



    def test_missing_claude_record_keeps_its_native_process_evidence(self) -> None:
        self.hosts({"trunk": labeled("trunk", "w4:p1", "trunk")})
        self.claude("trunk", "w4:p1")
        self.assertIsInstance(unit_lookup.labeled_sessions("show")["trunk"].claude, LiveClaude)
        (self.records / "trunk-1.json").unlink()
        found = unit_lookup.labeled_sessions("show")["trunk"]
        self.assertIsInstance(found.claude, ClaudeUnknown)
        self.assertEqual(found.place, herdr.HerdrPlace("trunk", "w4:p1"))

    def test_live_claude_wins_over_leftover_marks_on_another_pane(self) -> None:
        self.hosts({"old": labeled("old", "w3:p3Y", "trunk", CODEX_THREAD_ID=""),
                    "new": labeled("new", "w4:p1", "trunk")})
        self.claude("new", "w4:p1")
        found = unit_lookup.every_labeled_session()
        self.assertEqual(list(found.units["show"]), ["trunk"])
        self.assertIsInstance(found.units["show"]["trunk"].claude, LiveClaude)
        self.assertEqual(found.units["show"]["trunk"].place, herdr.HerdrPlace("new", "w4:p1"))

    def test_a_running_registered_claude_without_a_socket_remains_unknown(self) -> None:
        self.hosts({"trunk": labeled("trunk", "w4:p1", "trunk")})
        self.claude("trunk", "w4:p1")
        record = self.records / "trunk-1.json"
        data = cast(dict[str, object], json.loads(record.read_text()))
        data["procStart"] = sessions.process_start(os.getpid())
        data["messagingSocketPath"] = ""
        _ = record.write_text(json.dumps(data))
        found = unit_lookup.labeled_sessions("show")["trunk"]
        self.assertIsInstance(found.claude, ClaudeUnknown)



    def record(self, name: str, worktree: Path, started: float, *later: str) -> Path:
        """Write the record of a /unit:direct run in `worktree`, as the recorder would."""
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        owner = showrunner_relationship("history")
        row = staff.snapshot().unit_by_worktree(remote.local_machine(), worktree.resolve())
        if row is None:
            row = fixture_member("unit", name="history-" + name, parent=owner.id,
                                 worktree=staff.PresentWorktree(remote.local_machine(), worktree.resolve()))
        events = [{"event_type": "run_started", "main_agent": {"session_id": row.current_session}, "run_started_at": started},
                  *({"event_type": kind} for kind in later)]
        path = runs / f"{name}.jsonl"
        _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        root = self.root / "held"
        _ = cast(object, self.enterContext(mock.patch.multiple(unit_runs, RUN_ROOT=root, FORMER_RUN_ROOT=self.root / "former-held")))
        folder = root / name
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"owner_staff_id": row.id}))
        if "run_finished" in later:
            if isinstance(row.run, staff.OpenRun):
                _ = staff.clear_run(row.id, row.run.path)
        else:
            _ = staff.set_run(row.id, staff.OpenRun(folder))
        return path

    def held_folder(self, history: Path, director: str) -> Path:
        """Connect a temporary run folder to its recorded history."""
        root = self.root / "held"
        _ = cast(object, self.enterContext(mock.patch.multiple(
            unit_runs, RUN_ROOT=root, FORMER_RUN_ROOT=self.root / "former-held")))
        folder = root / history.stem
        folder.mkdir(parents=True, exist_ok=True)
        first = cast(dict[str, object], json.loads(history.read_text().splitlines()[0]))
        agent = cast(dict[str, str], first["main_agent"])
        source = staff.snapshot().by_member_reference(agent["session_id"])
        if source is None:
            source = next(row for row in staff.snapshot().find(kind="unit") if row.name == "history-" + history.stem)
        row = staff.snapshot().by_member_reference(director)
        if row is None:
            row = staff.update_current_session(source.current_session, staff.StaffId(director))
        elif row.worktree != source.worktree:
            location = source.worktree
            assert isinstance(location, staff.PresentWorktree)
            _ = staff.set_worktree(source.id, staff.RemovedWorktree(location.machine, location.path),
                                   expected=source.worktree)
            _ = staff.set_worktree(row.id, location, expected=row.worktree)
        parent = staff.snapshot().parent_of(row)
        assert parent is not None
        _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({
            "history_file": str(history), "owner_staff_id": row.id}))
        first["main_agent"] = {"session_id": director}
        later = history.read_text().splitlines()[1:]
        _ = history.write_text("\n".join([json.dumps(first), *later]) + "\n")
        if not any(cast(dict[str, object], json.loads(line)).get("event_type") == "run_finished" for line in later):
            _ = staff.set_run(row.id, staff.OpenRun(folder))
        return folder

    def test_run_state_follows_staff_without_a_history_rewrite(self) -> None:
        old = self.root / "old-folder"
        history = self.record("staff-run", old, 1.0, "run_finished")
        before = history.read_bytes(), history.stat().st_mtime_ns
        row = staff.snapshot().unit_by_worktree(remote.local_machine(), old)
        assert row is not None
        destination = self.root / "staff-folder"
        _ = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), destination), expected=row.worktree)
        current = unit_lookup.read_run_states()
        self.assertIs(current.state(destination), UnitState.RUN_FINISHED)
        self.assertIs(current.state(old), UnitState.STANDING_BY)
        self.assertEqual((history.read_bytes(), history.stat().st_mtime_ns), before)

    def test_unfinished_held_run_uses_the_staff_worktree(self) -> None:
        old = self.record("old-run", self.root / "old", 1.0, "run_finished")
        current = self.record("different-run-id", self.root / "new", 2.0)
        _ = self.held_folder(old, fixture_runtime_id("staff-director"))
        _ = self.held_folder(current, fixture_runtime_id("staff-director"))
        with mock.patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened:
            runs = unit_lookup.HeldRunLookup()
            found = runs.unfinished_held_run(fixture_runtime_id("staff-director"))
            history_opens = [call for call in opened.call_args_list if call.args[0] in (old, current)]
        # Read the current run header once.
        self.assertEqual(len(history_opens), 1)
        self.assertIsInstance(found, unit_lookup.HeldRun)
        assert isinstance(found, unit_lookup.HeldRun)
        self.assertEqual((found.path, found.worktree), (current, self.root / "new"))
        self.assertIs(unit_lookup.read_run_states().state(self.root / "old"), UnitState.STANDING_BY)
        self.assertIsInstance(runs.unfinished_held_run("missing-session"), unit_lookup.NoUnfinishedRun)
        old.unlink()
        current.unlink()
        self.assertEqual(runs.unfinished_held_run(fixture_runtime_id("staff-director")), found)

    def test_held_run_uses_the_supplied_staff_snapshot(self) -> None:
        history = self.record("run", self.root / "tree", 1.0)
        director = fixture_runtime_id("director")
        _ = self.held_folder(history, director)
        record = staff.snapshot()
        row = record.by_member_reference(director)
        assert row is not None
        _ = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), self.root / "later"), expected=row.worktree)
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("another staff read")):
            found = unit_lookup.HeldRunLookup(record=record).unfinished_held_run(director)
        self.assertIsInstance(found, unit_lookup.HeldRun)
        assert isinstance(found, unit_lookup.HeldRun)
        self.assertEqual((found.path, found.worktree), (history, self.root / "tree"))

    def test_recorded_replacement_director_owns_the_run_even_with_a_stale_staff_pointer(self) -> None:
        history = self.record("run", self.root / "tree", 1.0)
        lines = history.read_text().splitlines()
        first = cast(dict[str, object], json.loads(lines[0]))
        first["main_agent"] = {"session_id": fixture_runtime_id("original")}
        _ = history.write_text(json.dumps(first) + "\n")
        folder = self.held_folder(history, fixture_runtime_id("replacement"))
        owner = staff.local_snapshot().by_current_session(fixture_runtime_id("original"))
        if owner is None:
            owner = fixture_member("showrunner", name="original", runtime_id=fixture_runtime_id("original"))
        _ = staff.set_run(owner.id, staff.OpenRun(folder))
        runs = unit_lookup.HeldRunLookup()
        self.assertIsInstance(runs.unfinished_held_run(fixture_runtime_id("replacement")), unit_lookup.HeldRun)
        self.assertIsInstance(runs.unfinished_held_run(fixture_runtime_id("original")), unit_lookup.NoUnfinishedRun)

    def test_held_run_uses_the_staff_pointer_despite_a_history_finish_event(self) -> None:
        open_run = self.record("open-run", self.root / "tree", 1.0)
        latest = self.record("finished-run", self.root / "tree", 2.0, "run_finished")
        _ = self.held_folder(open_run, fixture_runtime_id("director"))
        finished_folder = self.held_folder(latest, fixture_runtime_id("director"))
        owner = staff.local_snapshot().by_current_session(fixture_runtime_id("director"))
        if owner is None:
            owner = fixture_member("showrunner", name="director", runtime_id=fixture_runtime_id("director"))
        _ = staff.set_run(owner.id, staff.OpenRun(finished_folder))
        runs = unit_lookup.HeldRunLookup()
        found = runs.unfinished_held_run(fixture_runtime_id("director"))
        self.assertIsInstance(found, unit_lookup.HeldRun)
        assert isinstance(found, unit_lookup.HeldRun)
        self.assertEqual(found.path, latest)
        self.assertIs(unit_lookup.read_run_states().state(self.root / "tree"), UnitState.RUNNING)

    def test_finished_held_run_is_not_eligible(self) -> None:
        history = self.record("finished", self.root / "tree", 1.0, "run_finished")
        _ = self.held_folder(history, fixture_runtime_id("director"))
        self.assertIsInstance(unit_lookup.HeldRunLookup().unfinished_held_run(fixture_runtime_id("director")), unit_lookup.NoUnfinishedRun)

    def test_held_history_read_failures_are_unknown_and_cached(self) -> None:
        history = self.record("run", self.root / "tree", 1.0)
        _ = self.held_folder(history, fixture_runtime_id("director"))
        for contents in (b"not JSON\n", b"\xff\n", b'{"event_type":"not_run_started"}\n',
                         b'{"event_type":"not_run_started","working_dir":"/tree"}\n\xff\n'):
            with self.subTest(contents=contents):
                _ = history.write_bytes(contents)
                runs = unit_lookup.HeldRunLookup()
                found = runs.unfinished_held_run(fixture_runtime_id("director"))
                self.assertIsInstance(found, unit_lookup.HeldRunUnknown)
                assert isinstance(found, unit_lookup.HeldRunUnknown)
                self.assertEqual(found.path, history)
                history.unlink()
                self.assertEqual(runs.unfinished_held_run(fixture_runtime_id("director")), found)
        self.assertIsInstance(unit_lookup.HeldRunLookup().unfinished_held_run(fixture_runtime_id("director")), unit_lookup.HeldRunUnknown)

    def test_unfinished_staff_pointer_run_precedes_an_alphabetically_earlier_run(self) -> None:
        first = self.record("a-run", self.root / "a-tree", 1.0)
        marked = self.record("z-run", self.root / "z-tree", 2.0)
        _ = self.held_folder(first, fixture_runtime_id("director"))
        folder = self.held_folder(marked, fixture_runtime_id("director"))
        owner = staff.local_snapshot().by_current_session(fixture_runtime_id("director"))
        if owner is None:
            owner = fixture_member("showrunner", name="director", runtime_id=fixture_runtime_id("director"))
        _ = staff.set_run(owner.id, staff.OpenRun(folder))
        found = unit_lookup.HeldRunLookup().unfinished_held_run(fixture_runtime_id("director"))
        self.assertIsInstance(found, unit_lookup.HeldRun)
        assert isinstance(found, unit_lookup.HeldRun)
        self.assertEqual(found.path, marked)

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

    def test_only_the_first_event_is_parsed(self) -> None:
        tree = self.root / "trunk"
        path = self.record("run", tree, 1.0)
        first = path.read_text(encoding="utf-8").strip()
        last = json.dumps({"event_type": "run_finished"})
        _ = path.write_text(first + "\nnot JSON\n[also broken\n" + last + "\n", encoding="utf-8")
        with mock.patch.object(unit_lookup, "json", wraps=json) as history_json:
            parses = cast(mock.MagicMock, history_json.loads)
            self.assertIs(unit_lookup.run_state(tree), UnitState.RUNNING)
        self.assertEqual(parses.call_args_list, [mock.call(first + "\n")])

    def test_last_event_larger_than_a_read_block(self) -> None:
        tree = self.root / "trunk"
        path = self.record("run", tree, 1.0)
        with path.open("a", encoding="utf-8") as handle:
            _ = handle.write(json.dumps({"event_type": "run_finished", "detail": "界" * (64 * 1024)},
                                        ensure_ascii=False) + "\n")
        self.assertIs(unit_lookup.run_state(tree), UnitState.RUNNING)

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
                if where == "first":
                    with self.assertRaises(UnicodeError):
                        _ = unit_lookup.read_run_states()
                else:
                    self.assertIs(unit_lookup.read_run_states().state(self.root / "trunk"), UnitState.RUNNING)


if __name__ == "__main__":
    _ = unittest.main()
