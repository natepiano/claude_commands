"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

from pathlib import Path

import json
import subprocess
import unittest
from typing import ClassVar, cast

from scripts.production.update_registration_test_support import RegistrationFixture, SCRIPT

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    needs_command_copy: ClassVar[bool] = True

    def test_register_reports_unexpected_usage_write_failure_and_completes(self) -> None:
        harness = """
import runpy
import sys
from unittest.mock import patch

script, *arguments = sys.argv[1:]
from scripts.usage import agents_log
sys.argv = [script, *arguments]
with patch.object(agents_log, 'append', side_effect=RuntimeError('recording failed\\ntry later')):
    runpy.run_path(script, run_name='__main__')
"""
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", harness, str(SCRIPT), "register", "--production", "example"],
            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "agents_log: recording failed try later\n")
        self.assertIn("register: ok", result.stdout)
        self.assertEqual(len([call for call in self.notifier_calls()
                              if call[:2] == ["new", f"showrunner-{self.fixture_staff_id()}"]]), 1)

    def test_copied_command_registers_and_records_showrunner_usage(self) -> None:
        result = self.run_copy(self.copied_command(), "register")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("register: ok", result.stdout)
        ledger = self.state / "usage/agents.jsonl"
        records = [cast(dict[str, object], json.loads(line))
                   for line in ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["id"], records[0]["role"]), ("current-session-id", "showrunner"))
        self.assertNotIn("lens", records[0])


if __name__ == "__main__":
    _ = unittest.main()
