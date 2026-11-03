"""The queue's file-only label lookup stays independent of the process roster."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, cast
import unittest
from unittest import mock

if TYPE_CHECKING:
    from scripts.message.sessions import SessionRecord


class FileOnlyLabelTests(unittest.TestCase):
    def test_failed_discovery_keeps_healthy_rows_and_reports_each_problem_once(self) -> None:
        from scripts.message import unit_labels
        from scripts.production import showrunners

        healthy = showrunners.Showrunner(session="healthy", socket="", slug="healthy",
                                         zone="UTC", doc="/fixture/healthy-production.md")
        failure = showrunners.ShowrunnerLookupFailure("missing", "session records are unreadable")
        discovery = showrunners.ShowrunnerDiscovery([healthy], (failure,))
        for emits_diagnostic in (False, True):
            with self.subTest(emits_diagnostic=emits_diagnostic):
                def discover(*arguments: object, **keywords: object) -> showrunners.ShowrunnerDiscovery:
                    del arguments, keywords
                    if emits_diagnostic:
                        print(failure.diagnostic, file=sys.stderr)
                    return discovery

                problems: list[str] = []
                with (tempfile.TemporaryDirectory() as directory,
                      mock.patch.object(showrunners, "SESSIONS_DIR", Path(directory)),
                      mock.patch.object(showrunners, "registered_showrunners", side_effect=discover)):
                    rows = unit_labels.registered_showrunners({
                        "NOTIFIER_STATE_DIR": directory, "NOTIFIER_SESSIONS_DIR": directory,
                    }, problems, known_sessions={})
                self.assertEqual(rows, [healthy])
                self.assertEqual(problems, [f"showrunners: {failure.diagnostic}"])

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
