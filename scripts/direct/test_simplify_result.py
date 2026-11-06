"""Check simplify result rules without starting a complete review."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest.mock import patch

from scripts.direct import simplify_result
from scripts.lib.nix_env_test_support import NixFixture, ROOT
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class SimplifyResultTests(unittest.TestCase):
    directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def review_files(self) -> simplify_result.ReviewFiles:
        return simplify_result.ReviewFiles(self.directory / "findings.txt", self.directory / "status",
                                           self.directory / "agent.log", self.directory / "summary.txt")

    def check_options(self, report: Path, log: Path) -> list[str]:
        files = self.review_files()
        return ["--findings-file", str(report), "--log-file", str(log), "--status-file", str(files.status),
                "--summary-file", str(files.summary)]

    def test_report_validation_accepts_real_review_headings_and_preambles(self) -> None:
        for report in (
            "**Simplify review: aws-unit, phase 8**\n\nScope: ...\n\n1. **`scripts/aws_target/remote.py:216`**",
            "# Simplify review: phase 101y\n\nI found 16 in-phase items...\n\n### In-phase\n\n1. **minor** · ...",
            "Verification is done. I am writing the report now.\n\n1. **`scripts/review_links/change_browser.py:35-38`** — `in-phase`",
            "# Review\n\nPass\n\nAgent results",
        ):
            with self.subTest(report=report):
                self.assertEqual(simplify_result.validate(report, "", 4), "")

    def test_extracted_errors_do_not_scan_the_agent_log_again(self) -> None:
        with patch.object(simplify_result, "claude_result", side_effect=AssertionError("repeated scan")):
            self.assertEqual(simplify_result.validate("ERROR: credentials expired", "large log", 4), "credentials expired")

    def test_failure_reason_keeps_the_stated_reason_before_report_and_log_evidence(self) -> None:
        for stated, report, log, expected in (
            ("stopped", "ERROR: report", "ERROR: log", "stopped"),
            ("", "ERROR: report", "ERROR: log", "report"),
            ("", "", "account detail\nERROR: no quota", "no quota"),
            ("", "", "", "the simplify review did not finish successfully (exit 9)"),
        ):
            with self.subTest(expected=expected):
                log_path = self.directory / "reason.log"
                _ = log_path.write_text(log)
                self.assertEqual(simplify_result.failure_reason(stated, report, log_path, 9), expected)

    def test_report_validation_preserves_unicode_whitespace_and_checks_agent_completion(self) -> None:
        cases: tuple[tuple[str, dict[str, int], str], ...] = (
            ("\u00a0Pass\u00a0", {}, ""),
            ("1.\u00a0Remove the duplicate.", {}, ""),
            ("", {}, "empty simplify report"),
            ("2. Wrong start", {}, "must start with Pass"),
            ("Pass", {"completed": 4, "failed": 0}, ""),
            ("Pass", {"completed": 3, "failed": 1}, "did not all complete"),
            ("Pass", {"completed": 4, "failed": 1}, "did not all complete"),
        )
        for report, stats, expected in cases:
            with self.subTest(report=report, stats=stats):
                log = json.dumps({"type": "result", "subagent_stats": stats})
                reason = simplify_result.validate(report, log, 4)
                if expected:
                    self.assertIn(expected, reason)
                else:
                    self.assertEqual(reason, "")

    def test_agent_completion_uses_the_configured_count_at_each_check(self) -> None:
        from scripts.lint.memory_test_support import write_config_fixture

        config = self.directory / "config"
        config.mkdir()
        report = self.directory / "report.txt"
        log = self.directory / "review.log"
        _ = log.write_text(json.dumps({"type": "result", "subagent_stats": {"completed": 2, "failed": 0}}))
        for agents, code in (("reuse efficiency", 0), ("reuse efficiency altitude", 1)):
            with self.subTest(agents=agents):
                _ = write_config_fixture(config / "direct.conf", f"SIMPLIFY_AGENTS={agents}\n")
                _ = report.write_text("Pass")
                result = subprocess.run([
                    sys.executable, str(ROOT / "scripts/direct/simplify_result.py"), "check",
                    *self.check_options(report, log),
                ], env={**os.environ, "CONF_DIR": str(config)}, capture_output=True, text=True,
                    check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, code, result.stderr)
                if code:
                    self.assertIn("3 simplify agents", result.stdout)
                    self.assertIn("ERROR:", report.read_text())

    def preparation_inputs(self) -> tuple[Path, Path, Path, Path]:
        from scripts.lint.memory_test_support import write_config_fixture

        config = self.directory / "config"
        _ = write_config_fixture(config / "direct.conf", "SIMPLIFY_AGENTS=reuse altitude\n")
        review = config / "review"
        review.mkdir()
        _ = (review / "simplify.md").write_text("Keep these agents: $agents.\n")
        diff = self.directory / "phase.diff"
        _ = diff.write_text("+change\n")
        source = self.directory / "source.md"
        _ = source.write_text("User instructions")
        return config, diff, source, self.directory / "prompt.md"

    def test_a_failed_prompt_publication_preserves_the_previous_complete_prompt(self) -> None:
        config, diff, source, prompt = self.preparation_inputs()
        _ = prompt.write_text("Previous complete prompt")
        with patch.dict(os.environ, {"CONF_DIR": str(config)}):
            with patch("scripts.lib.atomic_file.os.replace", side_effect=PermissionError("publication refused")):
                with self.assertRaisesRegex(OSError, "publication refused"):
                    _ = simplify_result.prepare(diff, self.directory, source, prompt, self.review_files())
        self.assertEqual(prompt.read_text(), "Previous complete prompt")

    def test_preparation_uses_the_configured_agent_list_for_the_prompt(self) -> None:
        config, diff, source, prompt = self.preparation_inputs()
        with patch.dict(os.environ, {"CONF_DIR": str(config)}):
            _ = simplify_result.prepare(diff, self.directory, source, prompt, self.review_files())
        self.assertEqual((self.directory / "prompt.md").read_text(),
                         f"/simplify {diff}\n\nKeep these agents: reuse,altitude.\n\n\nUser instructions\n")
        self.assertEqual(source.read_text(), "User instructions")

    def test_finish_copies_the_report_and_prints_a_failure_reason_once(self) -> None:
        files = self.review_files()
        _ = files.findings.write_text("Pass")
        _ = files.status.write_text("reviewed\n")
        with contextlib.redirect_stdout(io.StringIO()):
            state = simplify_result.finish(files, "", 0)
        self.assertEqual(state, "reviewed")
        self.assertEqual(Path(files.summary).read_text(), "Pass")
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            state = simplify_result.finish(files, "stopped", 143)
        self.assertEqual(state, "error")
        self.assertEqual(errors.getvalue(), "simplify: error: stopped\n")
        self.assertEqual(files.findings.read_text(), "ERROR: stopped\n")
        self.assertEqual(files.summary.read_text(), files.findings.read_text())
        self.assertEqual(files.status.read_text(), "reviewed\n")

    def test_empty_reports_preserve_account_failure_reasons_from_the_log(self) -> None:
        self.assertEqual(simplify_result.validate("", "ERROR: no claude account has room", 4),
                         "no claude account has room")
        self.assertEqual(simplify_result.validate("ERROR: weekly limit", json.dumps({
            "type": "result", "subagent_stats": {"completed": 0, "failed": 4}}), 4), "weekly limit")


class ClaudeQuotaSelectionTests(NixFixture):
    def test_other_account_modes_keep_stdout_empty_when_selection_fails(self) -> None:
        result = subprocess.run([
            "bash", "-c", 'source "$1"; claude_launch_environment',
            "select", str(ROOT / "scripts/agents/claude_account.sh"),
        ], env={**self.env, "CLAUDE_CONFIG_DIR": str(self.root / "missing")},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("caller has no numbered Claude account", result.stderr)
