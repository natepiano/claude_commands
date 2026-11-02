"""Check simplify result rules without starting a complete review."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import cast, override
import unittest
from unittest.mock import patch

from scripts.direct import simplify_result
from scripts.lib.nix_env_test_support import NixFixture, ROOT
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.whoami.account_test_support import write_claude_account


class SimplifyResultTests(unittest.TestCase):
    directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_failure_reason_keeps_the_stated_reason_before_report_and_log_evidence(self) -> None:
        for stated, report, log, expected in (
            ("stopped", "ERROR: report", "ERROR: log", "stopped"),
            ("", "ERROR: report", "ERROR: log", "report"),
            ("", "", "account detail\nERROR: no quota", "no quota"),
            ("", "", "", "the simplify review did not finish successfully (exit 9)"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(simplify_result.failure_reason(stated, report, log, 9), expected)

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
        for count, code in ((2, 0), (3, 1)):
            with self.subTest(count=count):
                _ = write_config_fixture(config / "direct.conf", f"SIMPLIFY_AGENT_COUNT={count}\n")
                _ = report.write_text("Pass")
                result = subprocess.run([
                    sys.executable, str(ROOT / "scripts/direct/simplify_result.py"), "check",
                    "--report-file", str(report), "--log-file", str(log),
                ], env={**os.environ, "CONF_DIR": str(config)}, capture_output=True, text=True,
                    check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, code, result.stderr)
                if code:
                    self.assertIn("3 simplify agents", result.stdout)
                    self.assertIn("ERROR:", report.read_text())

    def test_finish_saves_only_successful_results_and_copies_the_report(self) -> None:
        findings = self.directory / "review_findings_1_simplify.txt"
        status = self.directory / "review_status_simplify"
        cache = self.directory / "saved.json"
        summary = self.directory / "summary.txt"
        _ = findings.write_text("Pass")
        _ = status.write_text("reviewed\n")
        with patch.dict(os.environ, {"UNIT_REVIEW_CACHE_FILE": str(cache), "UNIT_REVIEW_SUMMARY_FILE": str(summary)}):
            with contextlib.redirect_stdout(io.StringIO()):
                simplify_result.finish(self.directory, self.directory, "phase", "", 0)
            self.assertEqual(json.loads(cache.read_text()), {"status": "reviewed", "summary": "Pass"})
            self.assertEqual(summary.read_text(), "Pass")
            cache.unlink()
            with patch.object(simplify_result, "send_failure_once") as send:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    simplify_result.finish(self.directory, self.directory, "phase", "stopped", 143)
                send.assert_called_once_with(self.directory, "phase", "stopped")
            self.assertEqual(status.read_text(), "error\n")
            self.assertFalse(cache.exists())

    def test_reuse_rejects_failed_records_and_restores_successful_evidence(self) -> None:
        cache = self.directory / "saved.json"
        with patch.dict(os.environ, {"UNIT_REVIEW_CACHE_FILE": str(cache), "UNIT_REVIEW_SUMMARY_FILE": ""}):
            _ = cache.write_text(json.dumps({"status": "error", "summary": "failed"}))
            self.assertFalse(simplify_result.reuse(self.directory))
            self.assertFalse(cache.exists())
            _ = cache.write_text(json.dumps({"status": "reviewed", "summary": "1. Remove the copy."}))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(simplify_result.reuse(self.directory))
            self.assertEqual((self.directory / "review_findings_1_simplify.txt").read_text(), "1. Remove the copy.")
            self.assertEqual((self.directory / "review_status_simplify").read_text(), "reviewed\n")

    def test_empty_reports_preserve_account_failure_reasons_from_the_log(self) -> None:
        self.assertEqual(simplify_result.validate("", "ERROR: no claude account has room", 4),
                         "no claude account has room")
        self.assertEqual(simplify_result.validate("ERROR: weekly limit", json.dumps({
            "type": "result", "subagent_stats": {"completed": 0, "failed": 4}}), 4), "weekly limit")

    def failure_message_arguments(self, proof: str) -> list[str]:
        from scripts.lint.memory_test_support import write_config_fixture

        _ = (self.directory / "progress_history_state.json").write_text('{"unit": "fixture-unit"}')
        _ = write_config_fixture(self.directory / "direct.conf", "")
        environment = {"CONF_DIR": str(self.directory)}
        if proof:
            environment["UNIT_REVIEW_PROOF"] = proof
        with patch.dict(os.environ, environment, clear=True):
            with patch("scripts.direct.simplify_result.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as send:
                simplify_result.send_failure_once(self.directory, "phase", "failure")
        return cast(list[str], send.call_args.args[0])

    def test_failure_messages_use_the_sender_beside_the_review_module(self) -> None:
        arguments = self.failure_message_arguments("")
        self.assertEqual(arguments[1], str(ROOT / "scripts/message/send.py"))
        self.assertIn("--repeat-minutes", arguments)

    def test_proof_failure_messages_mark_the_text_and_summary_only_when_enabled(self) -> None:
        for proof, prefix in (("", ""), ("1", "PROOF, no action: "), ("0", "")):
            with self.subTest(proof=proof):
                arguments = self.failure_message_arguments(proof)
                self.assertEqual(arguments[arguments.index("--summary") + 1],
                                 prefix + "URGENT: simplify review failed")
                self.assertEqual(arguments[arguments.index("--text") + 1],
                                 prefix + "URGENT: simplify review failed. Unit: fixture-unit. Phase: phase. Reason: failure")

    def test_claude_result_extraction_preserves_error_reasons_across_empty_events(self) -> None:
        log = self.directory / "claude.log"
        output = self.directory / "report.txt"
        cases: tuple[tuple[list[dict[str, object]], str, int], ...] = (
            ([{"type": "result", "errors": ["credentials expired"], "result": ""}], "credentials expired", 1),
            ([{"type": "result", "is_error": True, "result": "limit reached"}], "limit reached", 1),
            ([{"type": "result", "is_error": True, "result": "weekly limit"},
              {"type": "result", "result": ""}], "weekly limit", 1),
            ([{"type": "result", "result": ""}], "", 0),
            ([{"type": "result", "result": "Pass", "subagent_stats": {"completed": 3, "failed": 1}}], "Pass", 0),
        )
        for events, expected, code in cases:
            with self.subTest(events=events):
                _ = log.write_text("\n".join(json.dumps(event) for event in events))
                result = subprocess.run([
                    "bash", "-c", 'source "$1"; PY="$2"; agents_claude_extract_result "$3" "$4" direct.simplify',
                    "extract", str(ROOT / "scripts/agents/agent_exec.sh"), sys.executable, str(log), str(output),
                ], capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(expected, output.read_text())
                if code:
                    self.assertIn(expected, result.stderr)
                else:
                    self.assertEqual(output.read_text(), expected)


class ClaudeQuotaSelectionTests(NixFixture):
    def test_quota_selection_uses_the_soonest_reset_and_prints_the_selected_label(self) -> None:
        reset = datetime.now(timezone.utc) + timedelta(days=2)
        _ = (self.home / ".claude.json").write_text("{}")
        with patch.dict(os.environ, {**self.env, "AGENT_NOTES_DIR": str(self.root / "notes")}):
            write_claude_account("claude1", "one@example.invalid", 90, reset)
            write_claude_account("claude2", "two@example.invalid", 25, reset - timedelta(days=1))
            for first_left, second_left, chosen in ((90, 25, "claude2"), (90, 0, "claude1"), (0, 25, "claude2")):
                with self.subTest(first_left=first_left, second_left=second_left):
                    write_claude_account("claude1", "one@example.invalid", first_left, reset)
                    write_claude_account("claude2", "two@example.invalid", second_left, reset - timedelta(days=1))
                    result = subprocess.run([
                        "bash", "-c", 'source "$1"; claude_launch_environment quota',
                        "select", str(ROOT / "scripts/agents/claude_account.sh"),
                    ], env=dict(os.environ), capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(self.home / f".{chosen}"))
                    self.assertIn(f"Claude account {chosen}", result.stderr)

    def test_other_account_modes_keep_stdout_empty_when_selection_fails(self) -> None:
        result = subprocess.run([
            "bash", "-c", 'source "$1"; claude_launch_environment',
            "select", str(ROOT / "scripts/agents/claude_account.sh"),
        ], env={**self.env, "CLAUDE_CONFIG_DIR": str(self.root / "missing")},
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("caller has no numbered Claude account", result.stderr)
