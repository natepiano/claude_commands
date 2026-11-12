"""The queue's file-only label lookup stays independent of the process roster."""

from __future__ import annotations

import json
import io
import os
from contextlib import redirect_stderr
from pathlib import Path
import subprocess
import tempfile
from typing import TYPE_CHECKING, cast
import unittest
from unittest import mock
from scripts.production import staff
from scripts.agents import remote

if TYPE_CHECKING:
    from scripts.message.sessions import SessionRecord


class FileOnlyLabelTests(unittest.TestCase):
    def test_requested_owners_do_not_read_an_unrelated_remote_document(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import session_staff

        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "production.md"
            _ = document.write_text("# Production\n")
            owner = staff.StaffRow(
                staff.StaffId("owner"), "showrunner", "production", staff.StaffRoot(),
                current_session=staff.StaffId("owner-session"),
                doc=staff.LiveDoc(remote.local_machine(), document))
            member = staff.StaffRow(staff.StaffId("unit"), "unit", "chosen", staff.ReportsTo(owner.id),
                                    current_session=staff.StaffId("session"))
            foreign_owner = staff.StaffRow(
                staff.StaffId("foreign-owner"), "showrunner", "foreign", staff.StaffRoot(),
                current_session=staff.StaffId("foreign-owner-session"),
                doc=staff.LiveDoc(remote.other_machine(), Path("/unavailable/production.md")))
            foreign_member = staff.StaffRow(
                staff.StaffId("foreign-unit"), "unit", "foreign-unit", staff.ReportsTo(foreign_owner.id),
                current_session=staff.StaffId("foreign-session"))
            record = staff.StaffSnapshot((owner, member, foreign_owner, foreign_member))
            reads: list[Path] = []
            resolver = session_staff.RowResolver(record, dependencies=reads.append)
            with mock.patch("scripts.production.session_staff.subprocess.run", side_effect=AssertionError("remote read")):
                places = unit_labels.production_places(
                    [], set(), unit_labels.StaffDiagnostics(), record=record, resolver=resolver,
                    owners=unit_labels.label_owners({member.current_session}, record))
            self.assertEqual(set(places), {member.current_session})
            self.assertEqual(reads, [document])

    def test_local_label_lookup_never_reads_remote_staff_or_documents(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import session_staff

        owner = staff.StaffRow(
            staff.StaffId("owner"), "showrunner", "remote", staff.StaffRoot(),
            current_session=staff.StaffId("owner-session"),
            doc=staff.LiveDoc(remote.other_machine(), Path("/unavailable/production.md")))
        member = staff.StaffRow(staff.StaffId("unit"), "unit", "chosen", staff.ReportsTo(owner.id),
                                current_session=staff.StaffId("session"))
        native: SessionRecord = {
            "pid": os.getpid(), "sessionId": member.current_session, "name": member.name,
            "messagingSocketPath": "", "updatedAt": 0, "tmux": "", "formerNames": []}
        with mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((owner, member))), \
                mock.patch.object(staff, "snapshot", side_effect=AssertionError("remote staff read")), \
                mock.patch.object(unit_labels, "newest_sessions", return_value=[native]), \
                mock.patch("scripts.production.session_staff.subprocess.run", side_effect=AssertionError("remote read")):
            self.assertEqual(unit_labels.unit_labels_for(
                [member.current_session], {}, scope=session_staff.FactReadScope.LOCAL),
                {member.current_session: unit_labels.UnitLabels(member.name)})

    def test_recorded_owner_without_a_parent_shows_its_session_name(self) -> None:
        from scripts.message import unit_labels
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            member = fixture_member("producer", name="solo")
            with mock.patch.object(unit_labels, "newest_sessions", return_value=[]):
                labels = unit_labels.unit_labels_for([member.current_session], {})
            self.assertEqual(labels[member.current_session], unit_labels.UnitLabels("solo"))
            self.assertEqual(labels[member.current_session].owner_text, "solo")

    def test_recorded_unit_labels_survive_a_missing_session_registry_entry(self) -> None:
        from scripts.message import unit_labels
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            runner = fixture_member("showrunner", name="hana")
            unit = fixture_member("unit", name="demo", parent=runner.id)
            with mock.patch.object(unit_labels, "newest_sessions", return_value=[]):
                labels = unit_labels.unit_labels_for([unit.current_session], {})
            self.assertEqual(labels[unit.current_session], unit_labels.UnitLabels("demo", "hana", "demo"))

    def test_docless_holder_labels_stay_silent_across_repeated_reads(self) -> None:
        from scripts.message import unit_labels
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            owner = fixture_member("showrunner", name="docless")
            record: SessionRecord = {
                "pid": 123, "sessionId": owner.current_session, "name": "recorded-holder",
                "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": [],
            }
            errors = io.StringIO()
            with (mock.patch.object(unit_labels, "newest_sessions", return_value=[record]),
                  redirect_stderr(errors)):
                for _ in range(2):
                    self.assertEqual(unit_labels.unit_labels_for([owner.current_session], {}), {
                        owner.current_session: unit_labels.UnitLabels("docless"),
                    })
            self.assertEqual(errors.getvalue(), "")

    def test_recorded_fact_anomalies_are_deduplicated_across_readers(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import session_staff
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            owner = fixture_member("showrunner", name="docless")
            child = fixture_member("unit", name="child", parent=owner.id)
            record = staff.snapshot()
            self.assertEqual(record, staff.initial_snapshot())
            self.assertEqual(record.inputs, (str(Path(directory) / "staff.json"),))
            resolver = session_staff.RowResolver(record)
            diagnostics = unit_labels.StaffDiagnostics()
            for _ in range(2):
                self.assertEqual(unit_labels.registered_showrunners(diagnostics, known_sessions={}, resolver=resolver), [])
                self.assertEqual(unit_labels.production_places([], set(), diagnostics, record=record, resolver=resolver), {})
        self.assertEqual(diagnostics.problems, [])
        self.assertEqual(diagnostics.warnings, ["docless: the production document is not recorded",
                                               "child: the production document is not recorded"])
        self.assertEqual(diagnostics.excluded_sessions, {owner.current_session, child.current_session})

    def test_failed_discovery_keeps_healthy_rows_and_reports_each_problem_once(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import session_staff, showrunners
        from scripts.production.staff_test_support import fixture_runtime_id
        from scripts.agents import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = root / "healthy-production.md"
            _ = doc.write_text("# Healthy production\n")
            identity = staff.StaffId(fixture_runtime_id("healthy-owner"))
            healthy_owner = staff.StaffRow(identity, "showrunner", "healthy-owner", staff.StaffRoot(),
                                           current_session=identity, doc=staff.LiveDoc(remote.local_machine(), doc))
            missing_id = staff.StaffId(fixture_runtime_id("missing-owner"))
            missing = staff.StaffRow(missing_id, "showrunner", "missing-owner", staff.StaffRoot(),
                                     current_session=missing_id,
                                     doc=staff.LiveDoc(remote.local_machine(), root / "missing-production.md"))
            timer_path = root / f"showrunner-{identity}"
            timer_path.mkdir()
            _ = (timer_path / "conf").write_text(
                f"TARGET=session:{identity}\nCHECK=zsh /scripts/production_check.sh {identity}\n")
            record: SessionRecord = {
                "pid": 123, "sessionId": identity, "name": "healthy", "messagingSocketPath": "",
                "updatedAt": 1, "tmux": "", "formerNames": [],
            }
            healthy = showrunners.Showrunner(session="healthy-owner", socket="", name="healthy-owner",
                                             zone="UTC", doc=str(doc))
            resolver = session_staff.RowResolver(staff.StaffSnapshot((healthy_owner, missing)))
            problems: list[str] = []
            with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", root),
                  mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}),
                  mock.patch.object(remote, "local_machine", return_value=remote.local_machine())):
                rows = unit_labels.registered_showrunners(unit_labels.StaffDiagnostics(problems),
                                                         known_sessions={identity: record}, resolver=resolver)
            self.assertEqual(rows, [healthy])
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("missing-owner", problems[0])
            self.assertIn("missing-production.md", problems[0])

    def test_unreadable_timer_reports_its_owner_and_keeps_healthy_membership(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import session_staff, showrunners
        from scripts.production.staff_test_support import fixture_runtime_id
        from scripts.agents import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows: list[staff.StaffRow] = []
            for name in ("broken", "healthy"):
                document = root / f"{name}.md"
                _ = document.write_text("# Production\n")
                owner_id = staff.StaffId(fixture_runtime_id(name + "-owner"))
                rows.append(staff.StaffRow(owner_id, "showrunner", name + "-owner", staff.StaffRoot(),
                                          current_session=owner_id, doc=staff.LiveDoc(remote.local_machine(), document)))
                unit_id = staff.StaffId(fixture_runtime_id(name + "-unit"))
                rows.append(staff.StaffRow(unit_id, "unit", name, staff.ReportsTo(owner_id), current_session=unit_id,
                                          worktree=staff.PresentWorktree(remote.local_machine(), root / name)))
            snapshot = staff.StaffSnapshot(tuple(rows))

            def timer(row: staff.StaffRow, record: staff.StaffSnapshot) -> showrunners.TimerRegistration:
                del record
                if row.name == "broken-owner":
                    raise OSError("fixture timer cannot be read")
                return showrunners.TimerRegistration("healthy-alias", f"session:{row.current_session}", "UTC")

            with (mock.patch.object(showrunners, "timer_for", side_effect=timer),
                  mock.patch.object(remote, "local_machine", return_value=remote.local_machine())):
                for reader in ("registrations", "places"):
                    with self.subTest(reader=reader):
                        problems: list[str] = []
                        resolver = session_staff.RowResolver(snapshot)
                        if reader == "registrations":
                            registered = unit_labels.registered_showrunners(unit_labels.StaffDiagnostics(problems), known_sessions={}, resolver=resolver)
                            self.assertEqual([row["name"] for row in registered], ["healthy-owner"])
                        else:
                            places = unit_labels.production_places([], set(), unit_labels.StaffDiagnostics(problems), record=snapshot, resolver=resolver)
                            self.assertEqual(set(places), {rows[3].current_session})
                            self.assertEqual(places[rows[3].current_session].director, unit_labels.DirectsUnit("healthy"))
                        self.assertEqual(len(problems), 1, problems)
                        self.assertIn("broken-owner", problems[0])
                        self.assertIn("fixture timer cannot be read", problems[0])

    def test_production_places_reads_each_parent_timer_once(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import showrunners
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            doc = Path(directory) / "production.md"
            _ = doc.write_text("# Production\n")
            parents = [fixture_member("showrunner", name=name, doc=staff.LiveDoc(remote.local_machine(), doc))
                       for name in ("healthy", "broken")]
            for parent in parents:
                for number in range(3):
                    _ = fixture_member("unit", name=f"{parent.name}-{number}", parent=parent.id)
            record = staff.snapshot()
            problems: list[str] = []
            with mock.patch.object(showrunners, "timer_for", side_effect=[showrunners.NoTimer("fixture"),
                                                                       OSError("fixture timer failed")]) as timers:
                places = unit_labels.production_places([], set(), unit_labels.StaffDiagnostics(problems), record=record)
            self.assertEqual(timers.call_args_list, [mock.call(parent, record) for parent in parents])
            self.assertEqual({place.director for place in places.values()},
                             {unit_labels.DirectsUnit(f"healthy-{number}") for number in range(3)})
            self.assertEqual(len(problems), 1)
            self.assertIn("fixture timer failed", problems[0])
            renamed = staff.rename(parents[0].id, "renamed-owner")
            live: SessionRecord = {"pid": 123, "sessionId": renamed.current_session, "name": "stale registry name",
                "messagingSocketPath": "/private.sock", "updatedAt": 1, "tmux": "", "formerNames": []}
            record = staff.snapshot()
            with mock.patch.object(showrunners, "timer_for", return_value=showrunners.NoTimer("fixture")):
                registrations = unit_labels.registered_showrunners(unit_labels.StaffDiagnostics(),
                    known_sessions={renamed.current_session: live}, membership=record)
                places = unit_labels.production_places([live], set(), unit_labels.StaffDiagnostics(), record=record)
            self.assertEqual(registrations[0]["session"], "renamed-owner")
            self.assertEqual(registrations[0]["socket"], "/private.sock")
            self.assertEqual([place.showrunner for place in places.values() if place.director.unit.startswith("healthy-")],
                [unit_labels.UnderShowrunner("renamed-owner", "renamed-owner", f"session:{renamed.current_session}")] * 3)

    def test_unreadable_timer_file_does_not_remove_another_showrunners_rows(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import session_staff, showrunners
        from scripts.production.staff_test_support import fixture_runtime_id
        from scripts.agents import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows: list[staff.StaffRow] = []
            timers = root / "timers"
            for name in ("broken", "healthy"):
                document = root / f"{name}.md"
                _ = document.write_text("# Production\n")
                owner_id = staff.StaffId(fixture_runtime_id(name + "-owner"))
                rows.append(staff.StaffRow(owner_id, "showrunner", name + "-owner", staff.StaffRoot(),
                                          current_session=owner_id, doc=staff.LiveDoc(remote.local_machine(), document)))
                unit_id = staff.StaffId(fixture_runtime_id(name + "-unit"))
                rows.append(staff.StaffRow(unit_id, "unit", name, staff.ReportsTo(owner_id), current_session=unit_id,
                                          worktree=staff.PresentWorktree(remote.local_machine(), root / name)))
                timer_path = timers / f"showrunner-{owner_id}"
                timer_path.mkdir(parents=True)
                _ = (timer_path / "conf").write_text(
                    f"TARGET=session:{owner_id}\nCHECK=zsh /scripts/production_check.sh {owner_id}\n")
            unreadable = timers / f"showrunner-{rows[0].id}" / "conf"
            unreadable.unlink()
            unreadable.mkdir()
            snapshot = staff.StaffSnapshot(tuple(rows))
            with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", timers),
                  mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}),
                  mock.patch.object(remote, "local_machine", return_value=remote.local_machine())):
                for reader in ("registrations", "places"):
                    with self.subTest(reader=reader):
                        problems: list[str] = []
                        resolver = session_staff.RowResolver(snapshot)
                        if reader == "registrations":
                            registered = unit_labels.registered_showrunners(unit_labels.StaffDiagnostics(problems), known_sessions={}, resolver=resolver)
                            self.assertIn("healthy-owner", [row["name"] for row in registered])
                        else:
                            places = unit_labels.production_places([], set(), unit_labels.StaffDiagnostics(problems), record=snapshot, resolver=resolver)
                            self.assertIn(rows[3].current_session, places)
                            self.assertEqual(places[rows[3].current_session].director, unit_labels.DirectsUnit("healthy"))
                        self.assertEqual(len(problems), 1, problems)
                        self.assertIn("broken", problems[0])
                        self.assertIn("conf", problems[0])

    def test_wrapped_showrunner_children_have_no_current_production_place(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import session_staff, showrunners
        from scripts.production.staff_test_support import fixture_runtime_id
        from scripts.agents import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows: list[staff.StaffRow] = []
            for name in ("wrapped", "healthy"):
                document = root / f"{name}.md"
                _ = document.write_text("# Production\n")
                owner_id = staff.StaffId(fixture_runtime_id(name + "-owner"))
                document_state = (staff.WrappedDoc(remote.local_machine(), document) if name == "wrapped"
                                  else staff.LiveDoc(remote.local_machine(), document))
                rows.append(staff.StaffRow(owner_id, "showrunner", name, staff.StaffRoot(),
                                          current_session=owner_id, doc=document_state))
                unit_id = staff.StaffId(fixture_runtime_id(name + "-unit"))
                rows.append(staff.StaffRow(unit_id, "unit", name + "-child", staff.ReportsTo(owner_id), current_session=unit_id,
                                          worktree=staff.PresentWorktree(remote.local_machine(), root / name)))
            snapshot = staff.StaffSnapshot(tuple(rows))
            problems: list[str] = []
            with (mock.patch.object(showrunners, "timer_for", return_value=showrunners.NoTimer("private fixture")),
                  mock.patch.object(remote, "local_machine", return_value=remote.local_machine())):
                places = unit_labels.production_places([], set(), unit_labels.StaffDiagnostics(problems), record=snapshot,
                                                       resolver=session_staff.RowResolver(snapshot))
            self.assertEqual(set(places), {rows[3].current_session})
            self.assertEqual(places[rows[3].current_session].director, unit_labels.DirectsUnit("healthy-child"))
            self.assertEqual(problems, [])

    def test_a_known_session_resolves_without_loading_herdr(self) -> None:
        record: SessionRecord = {
            "pid": 123, "sessionId": "51e7edcb-739e-4f5c-a614-c06f4945a3dd", "name": "queue-reader-test",
            "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": [],
        }
        program = """
import dataclasses
import json
import sys
from unittest import mock
from scripts.message import unit_labels
from scripts.production import staff
from scripts.agents import remote
from scripts.production import session_staff

record = json.loads(sys.stdin.read())
with (mock.patch.object(unit_labels, "newest_sessions", return_value=[record]),
      mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())),
      mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources(()))):
    labels = unit_labels.unit_labels_for([record["sessionId"]], {
        "HERDR_PROC": sys.argv[1], "NOTIFIER_STATE_DIR": sys.argv[1],
        "NOTIFIER_SESSIONS_DIR": sys.argv[1],
    })
print(json.dumps({
    "labels": {key: dataclasses.asdict(value) for key, value in labels.items()},
    "herdr_modules": [name for name in sys.modules if name in {"herdr", "scripts.herdr.herdr"}],
}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", program, directory], input=json.dumps(record),
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                check=False, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(observed["labels"], {
            "51e7edcb-739e-4f5c-a614-c06f4945a3dd": {"session": "", "showrunner": "—", "unit": "—"},
        })
        self.assertEqual(observed["herdr_modules"], [])
