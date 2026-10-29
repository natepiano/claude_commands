"""Tests for asking gpt-6-astra to review a document, against a stand-in accounts tool."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import astra_review  # noqa: E402
from scripts.guilds import astra_work, roles, tips  # noqa: E402

SCRIPT = Path(__file__).with_name("astra_review.py")
# The stand-in for accounts.py: answers `role workers`, and for `exec` records its call and writes
# the review file Codex would write with -o.
STUB = """#!/usr/bin/env python3
import json, os, sys, time
arguments = sys.argv[1:]
if arguments[:2] == ["role", "workers"]:
    if os.environ.get("STUB_ROLE_FAILS"):
        print("accounts: no workers account is set", file=sys.stderr)
        sys.exit(1)
    print("codex7")
    sys.exit(0)
if arguments[:2] == ["env", "workers"]:
    print("CODEX_HOME=" + os.environ["STUB_CODEX_HOME"])
    sys.exit(0)
with open(os.environ["STUB_CALLS"], "w") as calls:
    json.dump({"argv": arguments, "stdin": sys.stdin.read(), "cwd": os.getcwd()}, calls)
print("codex: working")
if os.environ.get("STUB_ROLE_SNAPSHOT"):
    print("session id: thread-review-fixture", file=sys.stderr, flush=True)
    time.sleep(1)
    from pathlib import Path
    records = [json.loads(path.read_text()) for path in Path(os.environ["ASTRA_ROLES_STATE"]).glob("*.json")]
    with open(os.environ["STUB_ROLE_SNAPSHOT"], "w") as snapshot:
        json.dump({"records": records, "pid": os.getpid()}, snapshot)
if int(os.environ.get("STUB_EXIT", "0")):
    print("error: model gpt-6-astra is not available on this plan")
    sys.exit(int(os.environ["STUB_EXIT"]))
review = os.environ.get("STUB_REVIEW", "")
if review:
    with open(arguments[arguments.index("-o") + 1], "w") as written:
        written.write(review)
"""

REVIEWER_GUILD_BLOCK = (
    "Your guild is `reviewers`. Your head is the `reviewers` unit director in the `guilds` "
    "production on natedev. At skill start, use the Tips lock/pull rule for `~/rust/nate_style` and load "
    "`guilds/common/` and `guilds/reviewers/`; a Codex holder does this for its worker. Report to your "
    "head when the user corrects you, a phase needs repair round 4, a finding meets the Events repeat "
    "rule, you use a workaround for a tool fault or slow rule, or a method saves measured time. Send "
    "one report of at most 100 words, with evidence and result. Resolve the head from "
    "`message/roster.py --json` by production `guilds` and unit `reviewers`; send with `message/send.py` "
    "or SendMessage. A Codex worker puts the report in its reply for its holder to send. Send only to "
    "the head. Never wake an idle Codex member for guild advice."
)


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
                            "GUILD_ROLES_STATE": str(self.root / "guild-roles"),
                            "GUILD_OWNERSHIP_STATE": str(self.root / "guild-ownership"),
                            "GUILD_ROLES_HOST": "fixture-host",
                            "ASTRA_ROLES_STATE": str(self.root / "roles"), "STUB_CODEX_HOME": str(self.root / "codex-home"),
                            "STUB_REVIEW": "Verdict: clear.\n\n1. Section Why: the cause is not shown."}
        _ = self.environment.pop("CLAUDE_CODE_SESSION_ID", None)
        self.environment.update({"HOME": str(self.root), "GIT_CONFIG_GLOBAL": os.devnull,
                                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                                 "GIT_AUTHOR_NAME": "Review fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                                 "GIT_COMMITTER_NAME": "Review fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                                 "GUILD_TIPS_REPO": str(self.root / "tips")})
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.root / "origin.git"))
        self.git(self.root, "clone", str(self.root / "origin.git"), str(self.root / "tips"))
        for guild, text in (("common", "Fixture common report evidence."),
                            ("reviewers", "Fixture reviewer citation rule."),
                            ("designers", "Private designer fixture tip.")):
            folder = self.root / "tips/guilds" / guild
            folder.mkdir(parents=True)
            _ = (folder / "fixture.md").write_text(text + "\n", encoding="utf-8")
        self.git(self.root / "tips", "add", ".")
        self.git(self.root / "tips", "commit", "-m", "Fixture tips")
        self.git(self.root / "tips", "push", "-u", "origin", "main")

    def git(self, directory: Path, *arguments: str) -> None:
        result = subprocess.run(["git", "-C", str(directory), *arguments], env=self.environment,
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment, cwd=self.root,
                              capture_output=True, text=True, check=False)

    def call(self) -> Call:
        return cast(Call, json.loads((self.root / "calls.json").read_text(encoding="utf-8")))

    def saved_prompt(self) -> Path:
        argv = self.call()["argv"]
        return Path(argv[argv.index("-o") + 1]).with_name("prompt.md")

    def held_reviewer(self, status: str = "done") -> roles.HeldDesign:
        design_id = "00000000-0000-4000-8000-000000000016"
        design = roles.HeldDesign(roles.DesignPlacement(design_id, "fixture-host", 1), "fixture-repo",
                                  str(self.root), str(self.document), "proposal", "2026-10-21T12:00:00+00:00",
                                  roles.UnitHolder("demo", "proposal-unit"), roles.DesignUnbound(),
                                  (roles.RoleInstance("designer", "demo-proposal-astra-designer", roles.RoleNotOpened()),
                                   roles.RoleInstance("reviewer", "demo-proposal-astra-reviewer", roles.RoleOpen("held-thread"))))
        roles._save(design)  # pyright: ignore[reportPrivateUsage]
        mesh = roles.mesh_directory(design_id)
        mesh.mkdir(parents=True)
        _ = (mesh / "mesh_roster.json").write_text(json.dumps({"demo-proposal-astra-reviewer": {
            "thread_id": "held-thread", "status": status, "held_design_id": design_id, "held_role": "reviewer"}}))
        _ = (mesh / "mesh_server.json").write_text(json.dumps({"codex_home": str(self.root / "codex-home")}))
        return design

    def test_document_with_a_held_reviewer_resumes_that_thread_and_saves_each_reply(self) -> None:
        assignments: list[tuple[str, roles.ThreadAccessPolicy]] = []

        def dispatch(design: roles.HeldDesign, role: roles.Role, prompt: Path, reply: Path,
                     model: str, effort: str) -> None:
            del model, effort
            instance = next(item for item in design.roles if item.role == role)
            self.assertIsInstance(instance.lifecycle, roles.RoleOpen)
            self.assertTrue(prompt.is_file())
            if isinstance(instance.lifecycle, roles.RoleOpen):
                assignments.append((instance.lifecycle.thread_id, roles.access_policy(design, role)))
            _ = reply.write_text("Held reviewer evidence.\n")

        with patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": "fixture-holder"}), \
                patch.object(roles, "resolve_holder", return_value=roles.HolderReach("fixture-holder")), \
                patch.object(roles, "resolve_model", return_value=("fixture-model", "xhigh")), \
                patch.object(roles, "dispatch_turn", side_effect=dispatch), \
                patch.object(astra_review, "workers_label", side_effect=AssertionError("held review started a fresh exec")), \
                patch.object(tips, "load", return_value="Fixture reviewer tips"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            design = self.held_reviewer()
            for focus in ("first round", "repair round"):
                self.assertEqual(astra_review.main([str(self.document), focus]), 0)
            prompts = sorted(roles.design_directory(design.placement.design_id).glob("prompts/*.md"))
            self.assertEqual(len(prompts), 2)
            replies: list[str] = []
            for prompt in prompts:
                header = astra_work.read_header(prompt)
                self.assertIsInstance(header, astra_work.AstraWork)
                if isinstance(header, astra_work.AstraWork):
                    self.assertEqual((header.role, header.member), ("astra-reviewer", "demo-proposal-astra-reviewer"))
                    self.assertEqual(Path(header.reply_path).read_text(), "Held reviewer evidence.\n")
                    replies.append(header.reply_path)
        self.assertEqual(assignments, [("held-thread", roles.ReadOnlyReviewer()), ("held-thread", roles.ReadOnlyReviewer())])
        self.assertEqual(len(set(replies)), 2)
        self.assertIn("held reviewer", output.getvalue())
        self.assertFalse((self.root / "calls.json").exists())

    def test_busy_held_reviewer_is_reported_without_a_fresh_thread_or_prompt(self) -> None:
        with patch.dict(os.environ, {**self.environment, "CLAUDE_CODE_SESSION_ID": "fixture-holder"}), \
                patch.object(roles, "resolve_holder", return_value=roles.HolderReach("fixture-holder")), \
                patch.object(roles, "dispatch_turn", side_effect=AssertionError("busy role dispatched")), \
                patch.object(astra_review, "workers_label", side_effect=AssertionError("busy role used fresh exec")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            design = self.held_reviewer("running")
            self.assertEqual(astra_review.main([str(self.document)]), 1)
            self.assertEqual(list(roles.design_directory(design.placement.design_id).glob("prompts/*.md")), [])
        self.assertIn("busy", errors.getvalue())
        self.assertFalse((self.root / "calls.json").exists())

    def header(self) -> dict[str, object]:
        path = self.saved_prompt()
        first = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("<!-- astra_work "), first)
        self.assertTrue(first.endswith(" -->"), first)
        self.assertEqual(path.read_text(), self.call()["stdin"])
        return cast(dict[str, object], json.loads(first[len("<!-- astra_work "):-len(" -->")]))

    def test_standalone_prompt_saves_assignment_identity_and_utc_time(self) -> None:
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.header()
        prompt = self.saved_prompt()
        self.assertEqual({key: value for key, value in saved.items() if key != "assigned_at_utc"}, {
            "schema": 1, "role": "astra-reviewer", "member": str(prompt.parent),
            "document": str(self.document), "reply_path": str(prompt.with_name("review.md")),
            "work": {"kind": "standalone"},
        })
        stamp = datetime.fromisoformat(cast(str, saved["assigned_at_utc"]).replace("Z", "+00:00"))
        self.assertEqual(stamp.utcoffset(), timedelta(0))

    def test_phase_prompt_copies_the_selected_open_phase_and_history_identity(self) -> None:
        unit = self.root / "unit-run"
        unit.mkdir()
        history = self.root / "unit-history.jsonl"
        _ = (unit / "progress_history_state.json").write_text(json.dumps({
            "run_id": "saved-run-id", "history_file": str(history),
            "open_phases": [{"id": "2", "instance_id": "selected-instance", "status": "active"}],
            "phase": {"id": "4", "instance_id": "other-instance", "status": "active"},
        }), encoding="utf-8")
        result = self.run_script("--run-directory", str(unit), "--phase", "2", str(self.document), "scope")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.header()["work"], {
            "kind": "phase", "run_directory": str(unit), "run_id": "saved-run-id", "phase_id": "2",
            "phase_instance_id": "selected-instance", "history_file": str(history),
        })
        self.assertIn("focus on: scope", self.call()["stdin"])

    def test_unresolved_or_incomplete_phase_context_is_refused_before_codex(self) -> None:
        for flags in (("--run-directory", str(self.root), "--phase", "99"),
                      ("--run-directory", str(self.root)), ("--phase", "4")):
            with self.subTest(flags=flags):
                result = self.run_script(*flags, str(self.document))
                self.assertEqual((result.returncode, result.stdout), (2, ""))
                self.assertEqual(len(result.stderr.strip().splitlines()), 1, result.stderr)
                self.assertFalse((self.root / "calls.json").exists())

    def test_prompt_includes_the_exact_reviewers_block_and_committed_common_and_own_tips(self) -> None:
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.call()["stdin"]
        self.assertIn(REVIEWER_GUILD_BLOCK, prompt)
        self.assertIn("Load tips: ~/.claude/scripts/lib/py ~/.claude/scripts/guilds/tips.py load --guild reviewers", prompt)
        self.assertIn("guilds/common/fixture.md", prompt)
        self.assertIn("Fixture common report evidence.", prompt)
        self.assertIn("guilds/reviewers/fixture.md", prompt)
        self.assertIn("Fixture reviewer citation rule.", prompt)
        self.assertNotIn("Private designer fixture tip.", prompt)

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

    def test_the_reviewer_role_exists_during_success_and_failure_and_is_released_afterward(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = "sid-review-manager"
        snapshot = self.root / "during-review.json"
        self.environment["STUB_ROLE_SNAPSHOT"] = str(snapshot)
        for exit_code in (0, 3):
            with self.subTest(exit_code=exit_code):
                self.environment["STUB_EXIT"] = str(exit_code)
                result = self.run_script(str(self.document))
                self.assertEqual(result.returncode, 0 if exit_code == 0 else 1, result.stderr)
                during = cast(dict[str, object], json.loads(snapshot.read_text()))
                records = cast(list[dict[str, object]], during["records"])
                self.assertEqual(len(records), 1)
                self.assertEqual({key: value for key, value in records[0].items() if key != "held_at"}, {
                    "role": "reviewer", "thread_id": "thread-review-fixture",
                    "codex_home": self.environment["STUB_CODEX_HOME"], "manager": "sid-review-manager",
                    "pid": during["pid"], "session_dir": None, "worker": None,
                })
                self.assertFalse(list((self.root / "roles").glob("*.json")))

    def test_a_codex_caller_creates_no_reviewer_role(self) -> None:
        snapshot = self.root / "during-review.json"
        self.environment["STUB_ROLE_SNAPSHOT"] = str(snapshot)
        result = self.run_script(str(self.document))
        self.assertEqual(result.returncode, 0, result.stderr)
        during = cast(dict[str, object], json.loads(snapshot.read_text()))
        self.assertEqual(during["records"], [])
        self.assertFalse(list((self.root / "roles").glob("*.json")))


if __name__ == "__main__":
    _ = unittest.main()
