"""The queue's file-only label lookup stays independent of the process roster."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, cast
import unittest
from unittest import mock
from scripts.production import staff

if TYPE_CHECKING:
    from scripts.message.sessions import SessionRecord


class FileOnlyLabelTests(unittest.TestCase):
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
        from scripts.production import fake_showrunner, session_staff, showrunners
        from scripts.production.staff_test_support import fixture_runtime_id
        from scripts.shutdown import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = root / "healthy-production.md"
            _ = doc.write_text("# Healthy production\n")
            identity = staff.StaffId(fixture_runtime_id("healthy-owner"))
            healthy_owner = staff.StaffRow(identity, "showrunner", "healthy-owner", None,
                                           current_session=identity, doc=staff.LiveDoc("natedev", doc))
            missing_id = staff.StaffId(fixture_runtime_id("missing-owner"))
            missing = staff.StaffRow(missing_id, "showrunner", "missing-owner", None,
                                     current_session=missing_id,
                                     doc=staff.LiveDoc("natedev", root / "missing-production.md"))
            _ = fake_showrunner.write_timer(root, "healthy-alias", identity, "UTC", doc)
            record: SessionRecord = {
                "pid": 123, "sessionId": identity, "name": "healthy", "messagingSocketPath": "",
                "updatedAt": 1, "tmux": "", "formerNames": [],
            }
            healthy = showrunners.Showrunner(session="healthy", socket="", slug="healthy-alias",
                                             zone="UTC", doc=str(doc))
            resolver = session_staff.RowResolver(staff.StaffSnapshot((healthy_owner, missing)))
            problems: list[str] = []
            with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", root),
                  mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}),
                  mock.patch.object(remote, "local_machine", return_value="natedev")):
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
        from scripts.shutdown import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows: list[staff.StaffRow] = []
            for name in ("broken", "healthy"):
                document = root / f"{name}.md"
                _ = document.write_text("# Production\n")
                owner_id = staff.StaffId(fixture_runtime_id(name + "-owner"))
                rows.append(staff.StaffRow(owner_id, "showrunner", name + "-owner", None,
                                          current_session=owner_id, doc=staff.LiveDoc("natedev", document)))
                unit_id = staff.StaffId(fixture_runtime_id(name + "-unit"))
                rows.append(staff.StaffRow(unit_id, "unit", name, owner_id, current_session=unit_id,
                                          worktree=staff.PresentWorktree("natedev", root / name)))
            snapshot = staff.StaffSnapshot(tuple(rows))

            def timer(row: staff.StaffRow, record: staff.StaffSnapshot) -> showrunners.Timer:
                del record
                if row.name == "broken-owner":
                    raise OSError("fixture timer cannot be read")
                return showrunners.Timer("healthy-alias", f"session:{row.current_session}", root / "healthy.md", "UTC")

            with (mock.patch.object(showrunners, "timer_for", side_effect=timer),
                  mock.patch.object(remote, "local_machine", return_value="natedev")):
                for reader in ("registrations", "places"):
                    with self.subTest(reader=reader):
                        problems: list[str] = []
                        resolver = session_staff.RowResolver(snapshot)
                        if reader == "registrations":
                            registered = unit_labels.registered_showrunners(unit_labels.StaffDiagnostics(problems), known_sessions={}, resolver=resolver)
                            self.assertEqual([row["slug"] for row in registered], ["healthy-alias"])
                        else:
                            places = unit_labels.production_places([], set(), unit_labels.StaffDiagnostics(problems), record=snapshot, resolver=resolver)
                            self.assertEqual(set(places), {rows[3].current_session})
                            self.assertEqual(places[rows[3].current_session].director, unit_labels.DirectsUnit("healthy"))
                        self.assertEqual(len(problems), 1, problems)
                        self.assertIn("broken-owner", problems[0])
                        self.assertIn("fixture timer cannot be read", problems[0])

    def test_unreadable_timer_file_does_not_remove_another_showrunners_rows(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import fake_showrunner, session_staff, showrunners
        from scripts.production.staff_test_support import fixture_runtime_id
        from scripts.shutdown import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows: list[staff.StaffRow] = []
            timers = root / "timers"
            for name in ("broken", "healthy"):
                document = root / f"{name}.md"
                _ = document.write_text("# Production\n")
                owner_id = staff.StaffId(fixture_runtime_id(name + "-owner"))
                rows.append(staff.StaffRow(owner_id, "showrunner", name + "-owner", None,
                                          current_session=owner_id, doc=staff.LiveDoc("natedev", document)))
                unit_id = staff.StaffId(fixture_runtime_id(name + "-unit"))
                rows.append(staff.StaffRow(unit_id, "unit", name, owner_id, current_session=unit_id,
                                          worktree=staff.PresentWorktree("natedev", root / name)))
                _ = fake_showrunner.write_timer(timers, name + "-alias", owner_id, "UTC", document)
            unreadable = timers / "showrunner-broken-alias/conf"
            unreadable.unlink()
            unreadable.mkdir()
            snapshot = staff.StaffSnapshot(tuple(rows))
            with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", timers),
                  mock.patch.dict(os.environ, {"USER_ZONE": "UTC"}),
                  mock.patch.object(remote, "local_machine", return_value="natedev")):
                for reader in ("registrations", "places"):
                    with self.subTest(reader=reader):
                        problems: list[str] = []
                        resolver = session_staff.RowResolver(snapshot)
                        if reader == "registrations":
                            registered = unit_labels.registered_showrunners(unit_labels.StaffDiagnostics(problems), known_sessions={}, resolver=resolver)
                            self.assertIn("healthy-alias", [row["slug"] for row in registered])
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
        from scripts.shutdown import remote

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows: list[staff.StaffRow] = []
            for name in ("wrapped", "healthy"):
                document = root / f"{name}.md"
                _ = document.write_text("# Production\n")
                owner_id = staff.StaffId(fixture_runtime_id(name + "-owner"))
                document_state = (staff.WrappedDoc("natedev", document) if name == "wrapped"
                                  else staff.LiveDoc("natedev", document))
                rows.append(staff.StaffRow(owner_id, "showrunner", name, None,
                                          current_session=owner_id, doc=document_state))
                unit_id = staff.StaffId(fixture_runtime_id(name + "-unit"))
                rows.append(staff.StaffRow(unit_id, "unit", name + "-child", owner_id, current_session=unit_id,
                                          worktree=staff.PresentWorktree("natedev", root / name)))
            snapshot = staff.StaffSnapshot(tuple(rows))
            problems: list[str] = []
            with (mock.patch.object(showrunners, "timer_for", return_value=showrunners.NoTimer("private fixture")),
                  mock.patch.object(remote, "local_machine", return_value="natedev")):
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
from scripts.production import session_staff

record = json.loads(sys.stdin.read())
with (mock.patch.object(unit_labels, "newest_sessions", return_value=[record]),
      mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())),
      mock.patch.object(session_staff, "member_sources", return_value=session_staff.MemberSources({}, ()))):
    labels = unit_labels.unit_labels_for([record["sessionId"]], {
        "HERDR_PROC": sys.argv[1], "NOTIFIER_STATE_DIR": sys.argv[1],
        "NOTIFIER_SESSIONS_DIR": sys.argv[1],
    })
print(json.dumps({
    "labels": {key: dataclasses.asdict(value) for key, value in labels.items()},
    "herdr_modules": [name for name in sys.modules if name.rsplit(".", 1)[-1] == "herdr"],
}))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", program, directory], input=json.dumps(record),
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
                check=False, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual(observed["labels"], {
            "51e7edcb-739e-4f5c-a614-c06f4945a3dd": {"session": "queue-reader-test", "showrunner": "—", "unit": "—"},
        })
        self.assertEqual(observed["herdr_modules"], [])
