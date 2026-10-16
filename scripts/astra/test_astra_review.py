"""Tests for asking gpt-6-astra to review a document, against a stand-in accounts tool."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict, cast, override

SCRIPT = Path(__file__).with_name("astra_review.py")
# The stand-in for accounts.py: answers `role workers`, and for `exec` records its call and writes
# the review file Codex would write with -o.
STUB = """#!/usr/bin/env python3
import json, os, sys
arguments = sys.argv[1:]
if arguments[:2] == ["role", "workers"]:
    if os.environ.get("STUB_ROLE_FAILS"):
        print("accounts: no workers account is set", file=sys.stderr)
        sys.exit(1)
    print("codex7")
    sys.exit(0)
with open(os.environ["STUB_CALLS"], "w") as calls:
    json.dump({"argv": arguments, "stdin": sys.stdin.read(), "cwd": os.getcwd()}, calls)
print("codex: working")
if os.environ.get("STUB_EXIT"):
    print("error: model gpt-6-astra is not available on this plan")
    sys.exit(int(os.environ["STUB_EXIT"]))
review = os.environ.get("STUB_REVIEW", "")
if review:
    with open(arguments[arguments.index("-o") + 1], "w") as written:
        written.write(review)
"""


class Call(TypedDict):
    argv: list[str]
    stdin: str
    cwd: str


class AstraReviewTests(unittest.TestCase):
    root: Path = Path()
    document: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        stub = self.root / "accounts"
        _ = stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.document = self.root / "proposal.md"
        _ = self.document.write_text("# Proposal\n", encoding="utf-8")
        self.environment = {**os.environ, "ASTRA_REVIEW_ACCOUNTS": str(stub),
                            "ASTRA_REVIEW_DIR": str(self.root / "runs"), "STUB_CALLS": str(self.root / "calls.json"),
                            "STUB_REVIEW": "Verdict: clear.\n\n1. Section Why: the cause is not shown."}

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment, cwd=self.root,
                              capture_output=True, text=True, check=False)

    def call(self) -> Call:
        return cast(Call, json.loads((self.root / "calls.json").read_text(encoding="utf-8")))

    def test_the_review_runs_astra_read_only_on_the_workers_account_and_is_printed(self) -> None:
        result = self.run_script(str(self.document), "the", "rollout order")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.call()["argv"]
        self.assertEqual(argv[:3], ["exec", "codex7", "exec"])
        for flag, value in (("-m", "gpt-6-astra"), ("--sandbox", "read-only"), ("-C", str(self.root))):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        settings = [argv[index + 1] for index, word in enumerate(argv) if word == "-c"]
        self.assertEqual(settings, ['model_reasoning_effort="xhigh"', "model_auto_compact_token_limit=200000",
                                    'approval_policy="never"'])
        self.assertEqual(argv[-1], "-")
        self.assertIn("Verdict: clear.\n\n1. Section Why: the cause is not shown.", result.stdout)
        self.assertIn("workers account codex7", result.stdout)
        review = Path(argv[argv.index("-o") + 1])
        self.assertEqual(review.parent.parent, self.root / "runs")
        self.assertIn(f"Saved: {review}", result.stdout)

    def test_the_prompt_names_the_document_and_the_focus_and_forbids_building(self) -> None:
        self.assertEqual(self.run_script(str(self.document), "the rollout order").returncode, 0)
        prompt = self.call()["stdin"]
        self.assertIn(str(self.document), prompt)
        self.assertIn("focus on: the rollout order", prompt)
        for asked in ("Clarity", "Correctness", "Gaps", "Risks", "What you would change"):
            self.assertIn(asked, prompt)
        self.assertIn("Do not build, run tests", prompt)
        self.assertIn("section heading", prompt)

    def test_without_a_focus_the_prompt_asks_for_none(self) -> None:
        self.assertEqual(self.run_script(str(self.document)).returncode, 0)
        self.assertNotIn("focus on", self.call()["stdin"])

    def test_a_missing_document_is_refused_before_codex_starts(self) -> None:
        for arguments in ((str(self.root / "absent.md"),), ()):
            with self.subTest(arguments=arguments):
                result = self.run_script(*arguments)
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertFalse((self.root / "calls.json").exists())
        self.assertIn("no document at", self.run_script(str(self.root / "absent.md")).stderr)

    def test_a_codex_failure_prints_its_error_and_the_account(self) -> None:
        self.environment["STUB_EXIT"] = "3"
        result = self.run_script(str(self.document))
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("failed on workers account codex7 (exit 3)", result.stderr)
        self.assertIn("model gpt-6-astra is not available", result.stderr)

    def test_a_run_that_writes_no_review_fails(self) -> None:
        self.environment["STUB_REVIEW"] = ""
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no review was written", result.stderr)

    def test_an_unreadable_workers_account_fails_before_codex_starts(self) -> None:
        self.environment["STUB_ROLE_FAILS"] = "1"
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no workers account is set", result.stderr)
        self.assertFalse((self.root / "calls.json").exists())


if __name__ == "__main__":
    _ = unittest.main()
