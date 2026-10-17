"""Registration steps against a disposable production and notifier."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast, override


import update_registration as registration
import showrunners
import dailies_render
from add_unit import read_production

if TYPE_CHECKING:
    from scripts.notify import review
    from scripts.hooks.showrunner_footer import FooterState, read_pending_reviewed, PendingReviewUnavailableQuestion
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
    import review
    from showrunner_footer import FooterState, read_pending_reviewed, PendingReviewUnavailableQuestion

SCRIPT = Path(__file__).with_name("update_registration.py")


class QuestionRegistration(TypedDict):
    time: str
    text: str


NOTIFIER = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["REGISTRATION_TEST_STATE"])
args = sys.argv[1:]
with (root / "notifier-argv.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\\n")
instance = root / ("instance-" + args[1]) if len(args) > 1 else root / "missing"
if args[0] == "status":
    if not instance.exists():
        print(f"no such instance: {args[1]}", file=sys.stderr)
        raise SystemExit(1)
    mode = "stopped" if instance.read_text(encoding="utf-8") == "stopped" else "enabled"
    print(f"{args[1]} → session:test every 15 min, {mode}")
    print("next_due=1791334500 (2026-10-06 17:55 PDT)")
elif args[0] == "new":
    instance.touch()
    print("next_due=1791334500 (2026-10-06 17:55 PDT)")
elif args[0] == "remove":
    instance.unlink(missing_ok=True)
elif args[0] == "stop":
    _ = instance.write_text("stopped", encoding="utf-8")
else:
    raise SystemExit(2)
'''


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class RegistrationTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    state: Path = Path()
    config: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.state = self.root / "state"
        self.config = self.root / "showrunners.json"
        self.doc = self.checkout / "docs/plans/example-production.md"
        self.home.mkdir()
        self.checkout.mkdir()
        self.state.mkdir()
        notifier = self.home / ".claude/scripts/message/notifier.sh"
        notifier.parent.mkdir(parents=True)
        _ = notifier.write_text(NOTIFIER.replace("__PYTHON__", sys.executable), encoding="utf-8")
        notifier.chmod(0o755)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        # The command text names zsh; this shim runs only the disposable Python notifier.
        zsh = bin_dir / "zsh"
        _ = zsh.write_text("#!/bin/sh\nexec '" + sys.executable + "' \"$@\"\n", encoding="utf-8")
        zsh.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                    "REGISTRATION_TEST_STATE": str(self.state), "SHOWRUNNER_STATE_DIR": str(self.state / "showrunner"),
                    "NOTIFIER_STATE_DIR": str(self.state / "notifier"), "SHOWRUNNERS_CONFIG": str(self.config),
                    "USAGE_STATE_DIR": str(self.state / "usage"),
                    "MAC_TEST_STATE_DIR": str(self.state / "mac-test"),
                    "CLAUDE_CODE_SESSION_ID": "current-session-id"}
        _ = self.git("init", "-b", "production", str(self.checkout), cwd=self.root)
        _ = self.git("config", "user.name", "Registration Test")
        _ = self.git("config", "user.email", "registration@example.invalid")
        self.doc.parent.mkdir(parents=True)
        # A linked worktree, where `.git` is a file: herdr is never asked whether its unit is gone.
        alpha = self.root / "alpha"
        alpha.mkdir()
        _ = (alpha / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        _ = self.doc.write_text("\n".join((
            "# Production — example", "", "## Production Context", "",
            "- **Merge branch:** `production`", f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** first-showrunner", "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles", "- **Updates:** every 15 minutes", "",
            "## Units", "", "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| `alpha-unit` | `docs/alpha.md` | `{alpha}` | `alpha` | — | — |",
            "", "## Gates", "",
        )), encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        process = subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env if self.env else None,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout.strip()

    def run_command(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--production", str(self.doc)],
                              cwd=self.checkout, env=env or self.env, capture_output=True, text=True, check=False)

    def notifier_calls(self) -> list[list[str]]:
        path = self.state / "notifier-argv.jsonl"
        return ([cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
                if path.exists() else [])

    def outstanding(self) -> Path:
        path = self.state / "showrunner/outstanding/example.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def copied_command(self) -> Path:
        tree = Path(tempfile.mkdtemp(prefix="command-copy-", dir=self.root))
        _ = shutil.copytree(SCRIPT.parent, tree / "scripts/production")
        for name in ("build_hold", "build_targets", "buildlog", "herdr", "lib", "lint", "mac_test", "message", "usage", "whoami"):
            _ = shutil.copytree(SCRIPT.parent.parent / name, tree / "scripts" / name)
        hooks = tree / "scripts/hooks"
        hooks.mkdir(parents=True)
        _ = shutil.copy2(SCRIPT.parent.parent / "hooks/showrunner_footer.py", hooks)
        template = tree / "commands/showrunner/produce.md"
        template.parent.mkdir(parents=True)
        _ = shutil.copy2(SCRIPT.parents[2] / "commands/showrunner/produce.md", template)
        return tree / "scripts/production/update_registration.py"

    def run_copy(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *args, "--production", str(self.doc)],
                              cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False)

    def test_start_and_resume_retarget_only_updates_and_retire_old_session(self) -> None:
        doc_bytes = self.doc.read_bytes()
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok", first.stdout)
        initial_calls = self.notifier_calls()
        self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", "showrunner-example"]]), 1)
        updates = next(call for call in initial_calls if call[:2] == ["new", "showrunner-example"])
        self.assertEqual(updates[updates.index("--to") + 1], "session:current-session-id")
        self.assertIn("next_due", first.stdout)
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in initial_calls if call[:2] == ["new", instance]]), 1)

        new_env = {**self.env, "CLAUDE_CODE_SESSION_ID": "resume-session-id"}
        resumed = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        repeated = self.run_command("register", "--session", "resumed-showrunner", env=new_env)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        # The showrunner's name is written nowhere: no registry, no doc line, no commit.
        self.assertFalse(self.config.exists())
        self.assertEqual(self.doc.read_bytes(), doc_bytes)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        calls = self.notifier_calls()
        self.assertEqual(len([call for call in calls if call[:2] == ["new", "showrunner-example"]]), 3)
        updates = [call for call in calls if call[:2] == ["new", "showrunner-example"]]
        self.assertEqual([call[call.index("--to") + 1] for call in updates],
                         ["session:current-session-id", "session:resume-session-id", "session:resume-session-id"])
        for instance in ("stall-watch",):
            self.assertEqual(len([call for call in calls if call[:2] == ["new", instance]]), 1)

    def test_register_reports_unexpected_usage_write_failure_and_completes(self) -> None:
        harness = """
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

script, *arguments = sys.argv[1:]
sys.path.insert(0, str(Path(script).parent))
sys.path.insert(0, str(Path(script).parents[2]))
from scripts.usage import agents_log
sys.argv = [script, *arguments]
with patch.object(agents_log, 'append', side_effect=RuntimeError('recording failed\\ntry later')):
    runpy.run_path(script, run_name='__main__')
"""
        result = subprocess.run(
            [sys.executable, "-c", harness, str(SCRIPT), "register", "--production", str(self.doc)],
            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "agents_log: recording failed try later\n")
        self.assertIn("register: ok", result.stdout)
        self.assertEqual(len([call for call in self.notifier_calls()
                              if call[:2] == ["new", "showrunner-example"]]), 1)

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

    def test_on_demand_keeps_a_stopped_timer_as_the_showrunners_record(self) -> None:
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes", "- **Updates:** on demand"),
                                encoding="utf-8")
        _ = self.git("commit", "-qam", "dailies on demand")
        before = len(self.notifier_calls())
        resumed = self.run_command("register")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("register: ok — showrunner-example on demand: no scheduled dailies", resumed.stdout)
        calls = self.notifier_calls()[before:]
        self.assertIn(["new", "showrunner-example"], [call[:2] for call in calls])
        self.assertIn(["stop", "showrunner-example"], calls)
        self.assertNotIn(["remove", "showrunner-example"], calls)
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")
        again = self.run_command("register")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        # A re-register retargets the timer and stops it again, so it stays stopped.
        self.assertEqual([call[0] for call in self.notifier_calls()[before + len(calls):]],
                         ["status", "new", "stop", "status"])
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")

    def without_updates_line(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes\n", ""), encoding="utf-8")
        _ = self.git("commit", "-qam", "no Updates line")

    def test_a_new_production_with_no_updates_line_starts_quiet(self) -> None:
        self.without_updates_line()
        switch = self.state / "showrunner/footers-off/example"

        first = self.run_command("register")

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok — showrunner-example on demand: no scheduled dailies", first.stdout)
        self.assertIn("footer: ok — off — new production; /showrunner:footer on turns it on", first.stdout)
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")
        # Off on purpose: an empty switch, which a conversation pause's resume leaves off.
        self.assertEqual(switch.read_text(encoding="utf-8"), "")

    def test_a_restart_keeps_the_footer_it_has_on_or_off(self) -> None:
        switch = self.state / "showrunner/footers-off/example"
        first = self.run_command("register")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(switch.exists())
        switch.unlink()

        turned_on = self.run_command("register")

        self.assertEqual(turned_on.returncode, 0, turned_on.stdout + turned_on.stderr)
        self.assertNotIn("footer:", turned_on.stdout)
        self.assertFalse(switch.exists())
        _ = switch.write_text("", encoding="utf-8")
        kept_off = self.run_command("register")
        self.assertEqual(kept_off.returncode, 0, kept_off.stdout + kept_off.stderr)
        self.assertEqual(switch.read_text(encoding="utf-8"), "")

    def test_a_line_asking_for_a_schedule_still_schedules(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("every 15 minutes", "every 20 minutes"), encoding="utf-8")
        _ = self.git("commit", "-qam", "every 20 minutes")

        result = self.run_command("register")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updates = next(call for call in self.notifier_calls() if call[:2] == ["new", "showrunner-example"])
        self.assertEqual(updates[updates.index("--every") + 1], "20")
        self.assertNotIn(["stop", "showrunner-example"], self.notifier_calls())
        self.assertIn("next_due", result.stdout)

    def test_register_requires_the_environment_session_id(self) -> None:
        without_id = {key: value for key, value in self.env.items() if key != "CLAUDE_CODE_SESSION_ID"}
        missing_id = self.run_command("register", "--session", "first-showrunner", env=without_id)
        self.assertEqual(missing_id.returncode, 2)
        self.assertIn("register: failed", missing_id.stdout + missing_id.stderr)
        self.assertEqual(self.notifier_calls(), [])
        self.assertFalse(self.config.exists())
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_register_refuses_dirty_production_doc_without_side_effects(self) -> None:
        with self.doc.open("a", encoding="utf-8") as output:
            _ = output.write("\nA local note.\n")
        before = self.doc.read_bytes()
        result = self.run_command("register", "--session", "resumed-showrunner")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("register: failed — the production doc has uncommitted edits; commit or discard them first", result.stdout)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.notifier_calls(), [])
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_missing_prompt_markers_fail_before_registration_changes(self) -> None:
        for marker in ("The prompt:", "**A tick**"):
            with self.subTest(marker=marker):
                script = self.copied_command()
                template = script.parents[2] / "commands/showrunner/produce.md"
                _ = template.write_text(template.read_text(encoding="utf-8").replace(marker, "", 1), encoding="utf-8")
                before = self.doc.read_bytes()
                result = self.run_copy(script, "register", "--session", "resumed-showrunner")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("register: failed", result.stdout)
                self.assertIn(marker, result.stdout)
                self.assertEqual(self.doc.read_bytes(), before)
                self.assertFalse(self.config.exists())
                self.assertEqual(self.notifier_calls(), [])
                self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
                shutil.rmtree(script.parents[2])

    def test_registered_prompt_keeps_status_save_dailies_call_and_render_state(self) -> None:
        result = self.run_command("register", "--session", "first-showrunner")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        prompts = list((self.state / "showrunner").rglob("prompt.txt"))
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0].read_text(encoding="utf-8")
        self.assertIn("unit_status.txt", prompt)
        self.assertIn("/showrunner:dailies gantt", prompt)
        self.assertEqual(prompt.count("Pass `--render-state <SCRATCH>/dailies_state.json` to the builder"), 1)
        self.assertEqual(prompt.count("--render-state"), 1)
        self.assertIn("America/Los_Angeles", prompt)
        self.assertIn(f"--production {self.doc}", prompt)
        self.assertNotIn("first-showrunner", prompt)

    def test_time_converts_midnight_and_daylight_change_to_document_zone(self) -> None:
        midnight = self.run_command("time", "2026-10-07T06:30:00+00:00")
        self.assertEqual(midnight.returncode, 0, midnight.stdout + midnight.stderr)
        self.assertIn("23:30 PDT", midnight.stdout)
        winter = self.run_command("time", "2026-11-01T10:30:00+00:00")
        self.assertEqual(winter.returncode, 0, winter.stdout + winter.stderr)
        self.assertIn("02:30 PST", winter.stdout)
        self.assertNotIn("UTC", midnight.stdout + winter.stdout)

    def test_time_accepts_one_digit_hour_in_document_zone(self) -> None:
        result = self.run_command("time", "9:05")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"^time: ok — 09:05 P[DS]T\n$")

    def test_tenth_log_event_lists_every_doc_unit_without_state(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        doc = doc.replace("\n## Gates", "| `beta-unit` | `docs/beta.md` | `/tmp/beta` | `beta` | `beta-session` | — | — |\n\n## Gates")
        _ = self.doc.write_text(doc, encoding="utf-8")
        for number in range(1, 11):
            result = self.run_command("log", f"event {number}")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertEqual(log.count("### STATE "), 1)
        self.assertEqual(len(re.findall(r"^- \d{2}:\d{2} [A-Z]{3}: event \d+$", log, re.MULTILINE)), 10)
        self.assertIn("alpha-unit: not stated; last merged none; waits on not stated", log)
        self.assertIn("beta-unit: not stated; last merged none; waits on not stated", log)

    def test_state_block_matches_unit_and_merge_history_and_reads_outstanding(self) -> None:
        _ = self.git("commit", "--allow-empty", "-m", "Merge alpha-unit phase 2 (abc1234)")
        outstanding = self.outstanding()
        _ = outstanding.write_text(json.dumps([{"since": "2026-10-04T10:00", "text": "review colors"}]) + "\n",
                                   encoding="utf-8")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": [{"unit": "alpha-unit", "phase": "Phase 2 of 3: panels",
                                                    "wait": "review"}],
                                           "merges_held": ["alpha phase 2"],
                                           "open_for_user": ["stale state item"]}), encoding="utf-8")
        before = self.run_command("log", "compacting", "--state", str(state), "--before-compaction")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertIn("alpha-unit: Phase 2 of 3: panels; last merged phase 2 (abc1234); waits on review", log)
        self.assertIn("Merges accepted but held: alpha phase 2", log)
        self.assertIn("Open for the user: review colors", log)
        self.assertNotIn("stale state item", log)

    def test_invalid_state_fails_log_without_writing(self) -> None:
        log = self.checkout / "production.log"
        _ = log.write_text("existing line\n", encoding="utf-8")
        for key, value in (("unknown", 1), ("units", "wrong"), ("merges_held", "wrong")):
            with self.subTest(key=key):
                state = self.root / f"{key}.json"
                _ = state.write_text(json.dumps({key: value}), encoding="utf-8")
                before = log.read_bytes()
                result = self.run_command("log", "event", "--state", str(state), "--before-compaction")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"log: failed — {state}: {key} must be", result.stdout)
                self.assertEqual(log.read_bytes(), before)

    def question_state(self, items: list[dict[str, str] | str]) -> Path:
        path = self.root / "questions.json"
        _ = path.write_text(json.dumps({"open_for_user": items}))
        return path

    def run_question_footer(self, state: Path) -> str:
        with (mock.patch.dict(os.environ, self.env),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state / "notifier"),
              mock.patch.object(registration, "footer_state", return_value=FooterState.OFF),
              redirect_stdout(io.StringIO()) as output):
            registration.footer(read_production(self.doc), state, False)
        return output.getvalue()

    def question_instance(self) -> Path:
        instance = self.state / "notifier/showrunner-example"
        instance.mkdir(parents=True, exist_ok=True)
        _ = (instance / "conf").write_text("TARGET=session:current-session-id\n")
        return instance

    def test_new_object_question_requires_full_text_but_log_remains_a_parser(self) -> None:
        state = self.question_state([{"text": "Choose layout"}])
        parsed = registration.judgment_state(state, "log")
        self.assertIsInstance(parsed.outstanding, registration.ReplaceOutstanding)
        with self.assertRaisesRegex(registration.RegistrationFailure, "a new question needs its full text"):
            _ = self.run_question_footer(state)
        self.assertFalse(self.outstanding().exists())
        self.assertFalse((self.state / "notifier/showrunner-example/pending_reviewed.json").exists())

    def test_held_question_batch_writes_no_records_and_rerun_accepts_reviewed_text(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"Existing reminder"}]')
        history = outstanding.with_suffix(".history.jsonl")
        _ = history.write_text(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "text": "Earlier"}) + "\n")
        pending = self.state / "notifier/showrunner-example/pending_reviewed.json"
        _ = self.question_instance()
        _ = pending.write_text(json.dumps({"items": [{"kind": "reviewed", "text": "Earlier", "reviewed": "Earlier question?"}]}))
        before = [path.read_bytes() for path in (outstanding, history, pending)]
        full = "Build 42 failed.\nRetry (recommended) or stop?"
        state = self.question_state([{"text": "Retry build", "question": full},
                                     {"text": "Choose compiler", "question": "Use stable (recommended) or nightly?"}])
        with mock.patch.object(review, "review_question", side_effect=[
                review.QuestionAccepted(full),
                review.QuestionAwaitingFacts("Use stable (recommended) or nightly?", ("Which target?",))]) as reviewer:
            with self.assertRaisesRegex(registration.RegistrationFailure, "question held for review: Choose compiler: Which target"):
                _ = self.run_question_footer(state)
        self.assertEqual([path.read_bytes() for path in (outstanding, history, pending)], before)
        self.assertEqual(reviewer.call_args_list[0].args, (review.QuestionKey("example", "Retry build"), full))
        accepted = "Build 42 failed on natedev.\nRetry (recommended) or stop?"
        with mock.patch.object(review, "review_question", side_effect=[
                review.QuestionAccepted(accepted), review.QuestionAccepted("Use stable (recommended) or nightly?")]):
            output = self.run_question_footer(state)
        self.assertIn("reviewed question: " + accepted, output)
        rows = cast(list[dict[str, str]], json.loads(outstanding.read_text()))
        self.assertEqual(rows[0]["question"], full)
        self.assertEqual(rows[0]["reviewed"], accepted)
        saved = cast(dict[str, list[dict[str, str]]], json.loads(pending.read_text()))
        self.assertEqual([item["text"] for item in saved["items"]], ["Retry build", "Choose compiler"])
        lines = [cast(dict[str, str], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual(lines[1]["reviewed"], accepted)

    def test_question_rewrite_recheck_failure_keeps_registration_files_unchanged_and_revision(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text("[]")
        history = outstanding.with_suffix(".history.jsonl")
        _ = history.write_text("")
        pending = self.state / "notifier/showrunner-example/pending_reviewed.json"
        pending.parent.mkdir(parents=True)
        _ = pending.write_text('{"items":[]}')
        paths = (outstanding, history, pending)
        before = [path.read_bytes() for path in paths]
        original, rewritten = "Retry it?", "Retry build 42 (recommended) or stop?"
        state = self.question_state([{"text": "Retry build", "question": original}])
        responses = iter([
            review.VerdictRecord(verdict="rewrite", title="", action=None, body=rewritten, questions=[],
                failed_rules=[{"rule": "reader-starts-cold", "quote": original}]),
            review.VerdictRecord(verdict="ask", title="", action=None, body=rewritten, questions=["Which machine?"],
                failed_rules=[{"rule": "reader-starts-cold", "quote": "build 42"}]),
            review.VerdictRecord(verdict="send", title="", action=None, body=original, questions=[], failed_rules=[])])
        calls: list[tuple[list[str], float, dict[str, str]]] = []
        def runner(argv: list[str], *, timeout: float,
                   env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            calls.append((argv.copy(), timeout, env.copy()))
            if argv[0] == "zsh":
                return subprocess.CompletedProcess(argv, 0, "Question rules", "")
            if argv[0] == "bash":
                return subprocess.CompletedProcess(argv, 0, "agent=sonnet effort=high", "")
            self.assertEqual(Path(argv[0]).name, "claude")
            return subprocess.CompletedProcess(argv, 0, json.dumps(next(responses)), "")
        real_review = review.review_question
        def question_review(key: review.QuestionKey, question: str) -> review.QuestionOutcome:
            return real_review(key, question, runner=runner)
        with (mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.state / "notify")}),
              mock.patch.object(review, "GUIDE", self.root),
              mock.patch.object(review, "reviewer_environment", return_value=({}, "claude2")),
              mock.patch.object(review, "reviewer_binary", return_value=review.ResolvedReviewerBinary(self.root / "claude")),
              mock.patch.object(review, "review_question", side_effect=question_review)):
            with self.assertRaisesRegex(registration.RegistrationFailure, "Which machine"):
                _ = self.run_question_footer(state)
            self.assertEqual([path.read_bytes() for path in paths], before)
            self.assertTrue((self.state / "notify/questions" / (review.QuestionKey("example", "Retry build").digest + ".json")).exists())
            self.assertIn("reviewed question: " + original, self.run_question_footer(state))
        prompts = [call[0][-1] for call in calls if Path(call[0][0]).name == "claude"]
        self.assertIn("Revision: true", prompts[-1])
        self.assertEqual(len(prompts), 3)
        self.assertTrue(all(0 < call[1] <= review.review_timeout_seconds() for call in calls))

    def test_later_registration_keeps_reviewed_and_unavailable_questions_and_recall_is_multiline(self) -> None:
        _ = self.question_instance()
        full = "Build 42 failed.\nRetry (recommended) or stop?"
        unavailable = "Use stable (recommended) or nightly?"
        state = self.question_state([{"text": "Retry build", "question": full}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(full)):
            _ = self.run_question_footer(state)
        state = self.question_state(["Retry build", {"text": "Choose compiler", "question": unavailable}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionReviewUnavailable(unavailable, "timeout")):
            _ = self.run_question_footer(state)
        state = self.question_state([{"text": "Retry build", "after": "2099-01-01T08:00"}, "Choose compiler", "Reminder"])
        with mock.patch.object(review, "review_question", side_effect=AssertionError("reminder reviewed")):
            _ = self.run_question_footer(state)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual(rows[0]["reviewed"], full)
        self.assertEqual(rows[0]["question"], full)
        self.assertEqual(rows[1]["question"], unavailable)
        self.assertEqual(rows[1]["review_unavailable"], "timeout")
        self.assertNotIn("reviewed", rows[1])
        pending = self.state / "notifier/showrunner-example/pending_reviewed.json"
        saved = cast(dict[str, list[dict[str, str]]], json.loads(pending.read_text()))
        self.assertEqual([item["kind"] for item in saved["items"]], ["reviewed", "review unavailable"])
        with (mock.patch.dict(os.environ, self.env),
              mock.patch.object(showrunners, "user_facing", return_value=["example"]),
              redirect_stdout(io.StringIO()) as output):
            registration.open_items(read_production(self.doc))
        self.assertIn(full, output.getvalue())
        self.assertIn(unavailable, output.getvalue())
        self.assertIn("Reminder", output.getvalue())
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
        with mock.patch.dict(os.environ, self.env):
            self.assertIn(" ".join(full.split()), review.registered_questions(cutoff))
            _ = self.run_question_footer(self.question_state([]))
            self.assertIn(" ".join(full.split()), review.registered_questions(cutoff))

    def test_blank_reviewer_rewrite_stores_original_as_review_unavailable(self) -> None:
        question = "Retry build 42?"
        state = self.question_state([{"text": "Retry build", "question": question}])
        real_review = review.review_question
        for body in ("", " \n\t"):
            with self.subTest(body=body):
                self.outstanding().unlink(missing_ok=True)
                decisions: list[review.VerdictRecord] = [
                    review.VerdictRecord(verdict="rewrite", title="", action=None, body=body, questions=[],
                        failed_rules=[{"rule": "reader-starts-cold", "quote": question}]),
                    review.VerdictRecord(verdict="send", title="", action=None, body=body,
                        questions=[], failed_rules=[])]
                calls: list[tuple[list[str], float, dict[str, str]]] = []
                def runner(argv: list[str], *, timeout: float,
                           env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                    calls.append((argv.copy(), timeout, env.copy()))
                    if argv[0] == "zsh":
                        return subprocess.CompletedProcess(argv, 0, "Question rules", "")
                    if argv[0] == "bash":
                        return subprocess.CompletedProcess(argv, 0, "agent=sonnet effort=high", "")
                    self.assertEqual(Path(argv[0]).name, "claude")
                    return subprocess.CompletedProcess(argv, 0, json.dumps(decisions.pop(0)), "")
                def question_review(key: review.QuestionKey, text: str) -> review.QuestionOutcome:
                    return real_review(key, text, runner=runner)
                with (mock.patch.dict(os.environ, {"NOTIFY_STATE_DIR": str(self.state / "notify")}),
                      mock.patch.object(review, "GUIDE", self.root),
                      mock.patch.object(review, "reviewer_environment", return_value=({}, "claude2")),
                      mock.patch.object(review, "reviewer_binary", return_value=review.ResolvedReviewerBinary(self.root / "claude")),
                      mock.patch.object(review, "review_question", side_effect=question_review)):
                    output = self.run_question_footer(state)
                self.assertEqual(len([call for call in calls if Path(call[0][0]).name == "claude"]), 1)
                rows = dailies_render.read_outstanding(self.outstanding())
                self.assertEqual(len(rows), 1)
                self.assertIsInstance(rows[0], dailies_render.ReviewUnavailableOutstandingQuestion)
                row = rows[0]
                assert isinstance(row, dailies_render.ReviewUnavailableOutstandingQuestion)
                self.assertEqual(row.question, question)
                self.assertIn("rewrite needs nonempty text", row.reason)
                self.assertIn("reviewed question: " + question, output)

    def test_reviewed_question_without_instance_does_not_create_a_timer_directory(self) -> None:
        question = "Retry build 42?"
        state = self.question_state([{"text": "Retry build", "question": question}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(question)):
            output = self.run_question_footer(state)
        self.assertIn("reviewed question: " + question, output)
        rows = dailies_render.read_outstanding(self.outstanding())
        self.assertIsInstance(rows[0], dailies_render.ReviewedOutstandingQuestion)
        self.assertFalse((self.state / "notifier/showrunner-example").exists())
        self.assertEqual(showrunners.registered_showrunners(self.state / "notifier"), [])

    def test_duplicate_question_text_is_reviewed_and_recorded_once_using_first_occurrence(self) -> None:
        _ = self.question_instance()
        first = "Retry build 42?"
        state = self.question_state([
            {"text": "Retry build", "question": first},
            {"text": "Retry build", "question": "Use build 43?", "after": "2099-01-01T08:00"},
            "Reminder", "Reminder"])
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(first)) as reviewer:
            _ = self.run_question_footer(state)
        reviewer.assert_called_once_with(review.QuestionKey("example", "Retry build"), first)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual([row["text"] for row in rows], ["Retry build", "Reminder"])
        self.assertEqual(rows[0]["question"], first)
        self.assertNotIn("after", rows[0])
        history = [cast(dict[str, str], json.loads(line))
                   for line in self.outstanding().with_suffix(".history.jsonl").read_text().splitlines()]
        self.assertEqual([row["text"] for row in history], ["Retry build", "Reminder"])

    def test_replacing_outstanding_removes_closed_pending_questions_and_keeps_open_ones(self) -> None:
        instance = self.question_instance()
        question = "Retry build 42?"
        other = "Use stable?"
        state = self.question_state([{"text": "Retry build", "question": question},
                                     {"text": "Compiler", "question": other}])
        with mock.patch.object(review, "review_question", side_effect=[
                review.QuestionAccepted(question), review.QuestionReviewUnavailable(other, "timeout")]):
            _ = self.run_question_footer(state)
        _ = self.run_question_footer(self.question_state(["Compiler"]))
        self.assertEqual(read_pending_reviewed(instance), [
            PendingReviewUnavailableQuestion("Compiler", other, "timeout")])
        _ = self.run_question_footer(self.question_state([]))
        self.assertFalse((instance / "pending_reviewed.json").exists())

    def test_footer_state_updates_outstanding_only_when_open_items_are_present(self) -> None:
        outstanding = self.outstanding()
        saved = [{"since": "2026-10-01T08:00", "text": "review colors"}]
        _ = outstanding.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        absent = self.run_command("footer", "--state", str(state))
        self.assertEqual(absent.returncode, 0, absent.stdout + absent.stderr)
        self.assertEqual(outstanding.read_bytes(), before)
        _ = state.write_text(json.dumps({"open_for_user": ["review colors"]}), encoding="utf-8")
        retained = self.run_command("footer", "--state", str(state))
        self.assertEqual(retained.returncode, 0, retained.stdout + retained.stderr)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual(entries[0]["since"], "2026-10-01T08:00")
        _ = state.write_text(json.dumps({"open_for_user": []}), encoding="utf-8")
        cleared = self.run_command("footer", "--state", str(state))
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        self.assertEqual(json.loads(outstanding.read_text(encoding="utf-8")), [])

    def test_footer_records_only_new_questions_and_prunes_history_older_than_two_days(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text(json.dumps([{"since": "2026-10-01T08:00", "text": "Choose a compiler."}]))
        now = datetime.now(timezone.utc)
        history = outstanding.with_suffix(".history.jsonl")
        records = [QuestionRegistration(time=(now - timedelta(hours=49)).isoformat(), text="Expired question?"),
                   QuestionRegistration(time=(now - timedelta(hours=1)).isoformat(), text="Recent question?")]
        _ = history.write_text("\n".join(json.dumps(item) for item in records) + "\n")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": ["Choose a compiler.", "Approve the release."]}))
        for _ in range(2):
            result = self.run_command("footer", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = [cast(QuestionRegistration, json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([item["text"] for item in saved], ["Recent question?", "Approve the release."])
        self.assertEqual(saved[0], records[1])
        stamp = datetime.fromisoformat(saved[1]["time"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertGreaterEqual(stamp, now)

    def test_footer_keeps_registration_history_when_the_question_closes(self) -> None:
        outstanding = self.outstanding()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": ["Would you retry the build?"]}))
        opened = self.run_command("footer", "--state", str(state))
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        history = outstanding.with_suffix(".history.jsonl")
        before = history.read_bytes()
        _ = state.write_text(json.dumps({"open_for_user": []}))
        closed = self.run_command("footer", "--state", str(state))
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertEqual(json.loads(outstanding.read_text()), [])
        self.assertEqual(history.read_bytes(), before)
        saved = cast(QuestionRegistration, json.loads(before.decode().strip()))
        self.assertEqual(saved["text"], "Would you retry the build?")
        self.assertIsNotNone(datetime.fromisoformat(saved["time"]).tzinfo)

    def test_footer_rejects_bad_deferred_item_before_writing_outstanding(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"review colors"}]\n', encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "after": "tomorrow"}]}), encoding="utf-8")
        result = self.run_command("footer", "--state", str(state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("footer: failed", result.stdout)
        self.assertEqual(outstanding.read_bytes(), before)

    def test_footer_nothing_needed_is_explicit(self) -> None:
        ordinary = self.run_command("footer")
        self.assertEqual(ordinary.returncode, 0, ordinary.stdout + ordinary.stderr)
        self.assertNotIn(" - nothing needed", ordinary.stdout)
        quiet = self.run_command("footer", "--nothing-needed")
        self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr)
        self.assertIn("* no dailies scheduled - nothing needed", quiet.stdout)

    def test_footer_stopped_schedule_has_no_next_run(self) -> None:
        instance = self.state / "instance-showrunner-example"
        _ = instance.write_text("stopped", encoding="utf-8")
        stopped = self.run_command("footer")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("no dailies scheduled", stopped.stdout)
        self.assertNotIn("next dailies:", stopped.stdout)
        _ = instance.write_text("enabled", encoding="utf-8")
        enabled = self.run_command("footer")
        self.assertEqual(enabled.returncode, 0, enabled.stdout + enabled.stderr)
        self.assertIn("next dailies:", enabled.stdout)

    def test_footer_switch_off_still_stores_what_waits_on_the_user(self) -> None:
        switch = self.state / "showrunner/footers-off/example"
        switch.parent.mkdir(parents=True)
        switch.touch()
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"choose layout"}]')
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [
            "review colors", {"text": "choose layout", "after": "2099-01-01T08:00"}]}), encoding="utf-8")
        stored = self.run_command("footer", "--state", str(state))
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        self.assertEqual(stored.stdout.strip(), "footer: ok — footers off")
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual([entry["text"] for entry in entries], ["review colors", "choose layout"])
        self.assertEqual(entries[1]["after"], "2099-01-01T08:00")
        before = outstanding.read_bytes()
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        kept = self.run_command("footer", "--state", str(state))
        self.assertEqual(kept.stdout.strip(), "footer: ok — footers off")
        self.assertEqual(outstanding.read_bytes(), before)
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "after": "tomorrow"}]}),
                             encoding="utf-8")
        bad = self.run_command("footer", "--state", str(state))
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)
        self.assertIn("footer: failed", bad.stdout)
        self.assertEqual(outstanding.read_bytes(), before)

    def test_footer_switch_off_drops_all_output_and_on_renders_footer(self) -> None:
        switch = self.state / "showrunner/footers-off/example"
        switch.parent.mkdir(parents=True)
        switch.touch()
        off = self.run_command("footer")
        self.assertEqual(off.returncode, 0, off.stdout + off.stderr)
        self.assertEqual(off.stdout.strip(), "footer: ok — footers off")
        switch.unlink()
        on = self.run_command("footer")
        self.assertEqual(on.returncode, 0, on.stdout + on.stderr)
        self.assertIn("footer: ok", on.stdout)
        self.assertIn("update:", on.stdout)
        self.assertIn("no dailies scheduled", on.stdout)

    def timer(self, slug: str) -> None:
        """A production with a complete update timer, so the tree counts it."""
        instance = self.state / "notifier" / f"showrunner-{slug}"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{slug}-session\nCHECK=zsh /x/production_check.sh {self.doc}\n", encoding="utf-8")

    def attach(self, child: str, parent: str) -> None:
        edge = self.state / "showrunner" / child / "parent"
        edge.parent.mkdir(parents=True, exist_ok=True)
        _ = edge.write_text(parent + "\n", encoding="utf-8")

    def write_open_for(self, slug: str, *items: dict[str, str]) -> None:
        _ = self.outstanding().with_name(f"{slug}.json").write_text(json.dumps(list(items)), encoding="utf-8")

    def open_lines(self) -> list[str]:
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        return listed.stdout.splitlines()

    def test_footer_of_a_root_covers_lower_lists_and_a_lower_one_names_its_root(self) -> None:
        for slug in ("example", "kid"):
            self.timer(slug)
        self.attach("kid", "example")
        quiet = self.run_command("footer", "--nothing-needed")
        self.assertIn(" - nothing needed", quiet.stdout)
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today?"})
        counted = self.run_command("footer", "--nothing-needed")
        self.assertEqual(counted.returncode, 0, counted.stdout + counted.stderr)
        self.assertNotIn("nothing needed", counted.stdout)
        self.assertNotIn("merge today?", counted.stdout)
        _ = self.outstanding().with_name("kid.json").write_text("{bad", encoding="utf-8")
        unread = self.run_command("footer", "--nothing-needed")
        self.assertEqual(unread.returncode, 0, unread.stdout + unread.stderr)
        self.assertIn("* kid: needs-user list unreadable", unread.stdout.splitlines())
        self.timer("top")
        self.attach("example", "top")
        lower = self.run_command("footer")
        self.assertIn("* reports to top", lower.stdout.splitlines())
        self.assertNotIn("* kid: needs-user list unreadable", lower.stdout.splitlines())

    def test_open_lists_every_item_below_a_root_in_full_oldest_first(self) -> None:
        for slug in ("example", "kid", "grandkid", "stranger"):
            self.timer(slug)
        self.attach("kid", "example")
        self.attach("grandkid", "kid")
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"},
                            {"since": "2026-10-05T07:00", "text": "review colors", "after": "2099-01-02T08:00"})
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today, or after the release?"})
        self.write_open_for("grandkid", {"since": "2026-09-28T10:30", "text": "keep the old key binding?"})
        self.write_open_for("stranger", {"since": "2026-10-01T08:00", "text": "not this tree's"})
        self.assertEqual(self.open_lines(), [
            "1. grandkid, since Mon Sep 28 10:30: keep the old key binding?",
            "2. kid, since Tue Oct 06 08:00: merge today, or after the release?",
            "3. example, since Tue Oct 06 09:00: choose layout",
            "deferred until Fri Jan 02 08:00: example: review colors",
            "open: ok — 3 due, 1 deferred",
        ])

    def test_open_names_an_unreadable_list_and_still_lists_the_rest(self) -> None:
        for slug in ("example", "kid"):
            self.timer(slug)
        self.attach("kid", "example")
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"})
        _ = self.outstanding().with_name("kid.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "kid: needs-user list unreadable",
            "open: ok — 1 due, 0 deferred, 1 unreadable",
        ])

    def test_open_below_a_root_lists_only_its_own_items(self) -> None:
        for slug in ("top", "example", "kid"):
            self.timer(slug)
        self.attach("example", "top")
        self.attach("kid", "example")
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"})
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today?"})
        self.write_open_for("top", {"since": "2026-10-06T07:00", "text": "ship it?"})
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "open: ok — 1 due, 0 deferred",
        ])

    def test_open_with_nothing_attached_lists_its_own_items_or_none(self) -> None:
        self.assertEqual(self.open_lines(), ["open: ok — 0 due, 0 deferred"])
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"})
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "open: ok — 1 due, 0 deferred",
        ])


if __name__ == "__main__":
    _ = unittest.main()
