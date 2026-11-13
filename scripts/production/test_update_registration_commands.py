"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

from pathlib import Path

import json
import shutil
import subprocess
import sys
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

    def test_missing_prompt_markers_fail_at_delivery_without_registration_changes(self) -> None:
        script = self.copied_command()
        with self.subTest(prompt="body only"):
            template = script.parents[2] / "commands/showrunner/produce.md"
            _ = template.write_text("The prompt:\n\n> expected prompt\n> showrunner-timer-inside-prompt\n>\n> last line\n\n**A tick**\n")
            result = subprocess.run([sys.executable, str(script), "prompt", "--owner", self.owner_id],
                                    cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (0, "expected prompt\nshowrunner-timer-inside-prompt\n\nlast line\n", ""))
            result = subprocess.run([sys.executable, str(script), "delivery", "--owner", self.owner_id],
                                    cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (0, "showrunner-timer-example\nexpected prompt\nshowrunner-timer-inside-prompt\n\nlast line\n", ""))
        shutil.rmtree(script.parents[2])
        for marker in ("The prompt:", "**A tick**", "empty body"):
            with self.subTest(marker=marker):
                script = self.copied_command()
                template = script.parents[2] / "commands/showrunner/produce.md"
                document = ("The prompt:\n\n> \n>\n\n**A tick**\n" if marker == "empty body"
                            else template.read_text(encoding="utf-8").replace(marker, "", 1))
                _ = template.write_text(document, encoding="utf-8")
                registered = self.run_copy(script, "register", "--session", "resumed-showrunner")
                self.assertEqual(registered.returncode, 0, registered.stdout + registered.stderr)
                before = self.doc.read_bytes()
                calls = self.notifier_calls()
                result = subprocess.run([sys.executable, str(script), "prompt", "--owner", self.owner_id],
                                        cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("empty" if marker == "empty body" else marker, result.stdout + result.stderr)
                self.assertEqual(self.doc.read_bytes(), before)
                self.assertFalse(self.config.exists())
                self.assertEqual(self.notifier_calls(), calls)
                failure = ("prompt: failed — scheduled prompt is empty\n" if marker == "empty body"
                           else f"register: failed — {marker} marker is missing\n")
                self.assertEqual(result.stdout, failure)
                if marker == "empty body":
                    result = subprocess.run([sys.executable, str(script), "delivery", "--owner", self.owner_id],
                                            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (2, failure, ""))
                self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
                shutil.rmtree(script.parents[2])


if __name__ == "__main__":
    _ = unittest.main()
