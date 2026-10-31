"""The queue's file-only label lookup stays independent of the process roster."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, cast
import unittest

if TYPE_CHECKING:
    from scripts.message.sessions import SessionRecord


class FileOnlyLabelTests(unittest.TestCase):
    def test_a_known_session_resolves_without_loading_herdr(self) -> None:
        record: SessionRecord = {
            "pid": 123, "sessionId": "queue-reader", "name": "queue-reader-test",
            "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": [],
        }
        program = """
import dataclasses
import json
import sys
from unittest import mock
from scripts.message import unit_labels
from scripts.production import staff
import unit_marks

record = json.loads(sys.stdin.read())
with (mock.patch.object(unit_labels, "newest_sessions", return_value=[record]),
      mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())),
      mock.patch.object(unit_marks, "member_sources", return_value=unit_marks.MemberSources({}, ()))):
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
            "queue-reader": {"session": "queue-reader-test", "showrunner": "—", "unit": "—"},
        })
        self.assertEqual(observed["herdr_modules"], [])
