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
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
    import review

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
                    "MAC_TEST_STATE_DIR": str(self.state / "mac-test"), "NEXT_STATE_DIR": str(self.state / "next"),
                    "CLAUDE_CODE_SESSION_ID": "current-session-id", "NOTIFIER_SESSIONS_DIR": str(self.state / "sessions")}
        # Register refuses a session not named for its production's slug: both live sessions are named example.
        (self.state / "sessions").mkdir()
        for pid, session in ((os.getpid(), "current-session-id"), (os.getppid(), "resume-session-id")):
            _ = (self.state / "sessions" / f"{pid}.json").write_text(
                json.dumps({"pid": pid, "sessionId": session, "name": "example"}), encoding="utf-8")
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
        for name in ("build_hold", "build_targets", "buildlog", "direct", "herdr", "lib", "lint", "mac_test", "message", "usage", "whoami"):
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
                         ["new", "stop", "status"])
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")

    def without_updates_line(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes\n", ""), encoding="utf-8")
        _ = self.git("commit", "-qam", "no Updates line")

    def test_a_new_production_with_no_updates_line_starts_quiet(self) -> None:
        self.without_updates_line()
        first = self.run_command("register")

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("register: ok — showrunner-example on demand: no scheduled dailies", first.stdout)
        self.assertEqual((self.state / "instance-showrunner-example").read_text(encoding="utf-8"), "stopped")
        self.assertNotIn("footer", first.stdout)


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

    def test_register_warns_for_a_session_not_named_for_the_production_and_still_registers(self) -> None:
        record = self.state / "sessions" / f"{os.getpid()}.json"
        _ = record.write_text(json.dumps({"pid": os.getpid(), "sessionId": "current-session-id", "name": "other"}),
                              encoding="utf-8")
        result = self.run_command("register")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("register: warn", result.stdout)
        self.assertIn("production example has showrunner other", result.stdout)
        self.assertIn("/showrunner:rename plan example other", result.stdout)
        self.assertIn("register: ok", result.stdout)
        self.assertNotEqual(self.notifier_calls(), [])

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
        state_dir = self.state / "showrunner" / self.doc.name.removesuffix("-production.md") / "session"
        self.assertEqual(prompt.count(f"Pass `--render-state {state_dir}/dailies_state.json` to the builder"), 1)
        self.assertIn(f"unit_status.sh {state_dir}/unit_status ", prompt)
        self.assertNotIn("<STATE_DIR>", prompt)
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
                                           "open_for_user": ["stale state item"],
                                           "next": ["merge alpha phase 2 when its gate is green",
                                                    "send the restarts plan at 03:30"]}), encoding="utf-8")
        before = self.run_command("log", "compacting", "--state", str(state), "--before-compaction")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        log = (self.checkout / "production.log").read_text(encoding="utf-8")
        self.assertIn("alpha-unit: Phase 2 of 3: panels; last merged phase 2 (abc1234); waits on review", log)
        self.assertIn("Merges accepted but held: alpha phase 2", log)
        self.assertIn("Open for the user: review colors", log)
        self.assertNotIn("stale state item", log)
        self.assertIn("Next: merge alpha phase 2 when its gate is green; send the restarts plan at 03:30", log)

    def test_the_first_call_moves_the_scratchpad_state_and_rewrites_the_tick_prompt(self) -> None:
        slug = self.doc.name.removesuffix("-production.md")
        timer = self.state / "notifier" / f"showrunner-{slug}"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text("TARGET=session:old-showrunner\n", encoding="utf-8")
        scratch = self.root / "tmp" / "claude-1000" / "-project" / "old-showrunner" / "scratchpad"
        (scratch / "unit_status").mkdir(parents=True)
        _ = (scratch / "unit_status" / "blocks_open").write_text("alpha-unit\n", encoding="utf-8")
        _ = (scratch / "showrunner_state.json").write_text('{"merges_held": ["from scratch"]}\n', encoding="utf-8")
        target = self.state / "showrunner" / slug / "session"
        target.mkdir(parents=True)
        _ = (target / "showrunner_state.json").write_text('{"merges_held": ["kept"]}\n', encoding="utf-8")
        prompt = self.state / "showrunner" / slug / "prompt.txt"
        _ = prompt.write_text("an old prompt with <SCRATCH>/unit_status\n", encoding="utf-8")
        env = {**self.env, "SHOWRUNNER_SCRATCH_ROOT": str(self.root / "tmp")}

        result = self.run_command("log", "first event", env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"state-dir: ok — copied unit_status to {target}", result.stdout)
        self.assertEqual((target / "unit_status" / "blocks_open").read_text(encoding="utf-8"), "alpha-unit\n")
        self.assertIn("kept", (target / "showrunner_state.json").read_text(encoding="utf-8"))
        self.assertIn(f"unit_status.sh {target}/unit_status ", prompt.read_text(encoding="utf-8"))

        again = self.run_command("log", "second event", env=env)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertNotIn("state-dir", again.stdout)

    def test_invalid_state_fails_log_without_writing(self) -> None:
        log = self.checkout / "production.log"
        _ = log.write_text("existing line\n", encoding="utf-8")
        for key, value in (("unknown", 1), ("units", "wrong"), ("merges_held", "wrong"), ("next", ["two\nlines"])):
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

    def run_questions(self, state: Path) -> str:
        with (mock.patch.dict(os.environ, self.env),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state / "notifier"),
              redirect_stdout(io.StringIO()) as output):
            registration.questions(read_production(self.doc), state)
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
            _ = self.run_questions(state)
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
                _ = self.run_questions(state)
        self.assertEqual([path.read_bytes() for path in (outstanding, history, pending)], before)
        self.assertEqual(reviewer.call_args_list[0].args, (review.QuestionKey("example", "Retry build"), full))
        accepted = "Build 42 failed on natedev.\nRetry (recommended) or stop?"
        with mock.patch.object(review, "review_question", side_effect=[
                review.QuestionAccepted(accepted), review.QuestionAccepted("Use stable (recommended) or nightly?")]):
            output = self.run_questions(state)
        self.assertIn("reviewed question: " + accepted, output)
        rows = cast(list[dict[str, str]], json.loads(outstanding.read_text()))
        self.assertEqual(rows[0]["question"], full)
        self.assertEqual(rows[0]["reviewed"], accepted)
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
                _ = self.run_questions(state)
            self.assertEqual([path.read_bytes() for path in paths], before)
            self.assertTrue((self.state / "notify/questions" / (review.QuestionKey("example", "Retry build").digest + ".json")).exists())
            self.assertIn("reviewed question: " + original, self.run_questions(state))
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
            _ = self.run_questions(state)
        state = self.question_state(["Retry build", {"text": "Choose compiler", "question": unavailable}])
        with mock.patch.object(review, "review_question", return_value=review.QuestionReviewUnavailable(unavailable, "timeout")):
            _ = self.run_questions(state)
        state = self.question_state([{"text": "Retry build", "after": "2099-01-01T08:00"}, "Choose compiler", "Reminder"])
        with mock.patch.object(review, "review_question", side_effect=AssertionError("reminder reviewed")):
            _ = self.run_questions(state)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual(rows[0]["reviewed"], full)
        self.assertEqual(rows[0]["question"], full)
        self.assertEqual(rows[1]["question"], unavailable)
        self.assertEqual(rows[1]["review_unavailable"], "timeout")
        self.assertNotIn("reviewed", rows[1])
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
            _ = self.run_questions(self.question_state([]))
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
                    output = self.run_questions(state)
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
            output = self.run_questions(state)
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
            _ = self.run_questions(state)
        reviewer.assert_called_once_with(review.QuestionKey("example", "Retry build"), first)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual([row["text"] for row in rows], ["Retry build", "Reminder"])
        self.assertEqual(rows[0]["question"], first)
        self.assertNotIn("after", rows[0])
        history = [cast(dict[str, str], json.loads(line))
                   for line in self.outstanding().with_suffix(".history.jsonl").read_text().splitlines()]
        self.assertEqual([row["text"] for row in history], ["Retry build", "Reminder"])

    def test_questions_state_updates_outstanding_only_when_open_items_are_present(self) -> None:
        outstanding = self.outstanding()
        saved = [{"since": "2026-10-01T08:00", "text": "review colors"}]
        _ = outstanding.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        absent = self.run_command("questions", "--state", str(state))
        self.assertEqual(absent.returncode, 0, absent.stdout + absent.stderr)
        self.assertEqual(outstanding.read_bytes(), before)
        _ = state.write_text(json.dumps({"open_for_user": ["review colors"]}), encoding="utf-8")
        retained = self.run_command("questions", "--state", str(state))
        self.assertEqual(retained.returncode, 0, retained.stdout + retained.stderr)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual(entries[0]["since"], "2026-10-01T08:00")
        _ = state.write_text(json.dumps({"open_for_user": []}), encoding="utf-8")
        cleared = self.run_command("questions", "--state", str(state))
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        self.assertEqual(json.loads(outstanding.read_text(encoding="utf-8")), [])

    def test_questions_state_replaces_reports_only_when_report_to_user_is_present(self) -> None:
        written = self.outstanding().parent.parent / "reports/example.json"
        state = self.root / "judgment.json"
        report = {"text": "hanadocs is back", "until": "2026-10-17T09:00"}
        _ = state.write_text(json.dumps({"report_to_user": [report]}), encoding="utf-8")
        saved = self.run_command("questions", "--state", str(state))
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), [report])
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        kept = self.run_command("questions", "--state", str(state))
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), [report])
        for bad in ([{"text": "x", "until": "tomorrow"}], [{"text": "", "until": "2026-10-17T09:00"}],
                    [{"text": "x", "until": "2026-10-17T09:00", "question": "?"}], "x"):
            with self.subTest(bad=bad):
                _ = state.write_text(json.dumps({"report_to_user": bad}), encoding="utf-8")
                refused = self.run_command("questions", "--state", str(state))
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn("report_to_user", refused.stdout)
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), [report])

    def test_questions_records_only_new_questions_and_prunes_history_older_than_two_days(self) -> None:
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
            result = self.run_command("questions", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = [cast(QuestionRegistration, json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual([item["text"] for item in saved], ["Recent question?", "Approve the release."])
        self.assertEqual(saved[0], records[1])
        stamp = datetime.fromisoformat(saved[1]["time"])
        self.assertIsNotNone(stamp.tzinfo)
        self.assertGreaterEqual(stamp, now)

    def test_questions_keeps_registration_history_when_the_question_closes(self) -> None:
        outstanding = self.outstanding()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": ["Would you retry the build?"]}))
        opened = self.run_command("questions", "--state", str(state))
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        history = outstanding.with_suffix(".history.jsonl")
        before = history.read_bytes()
        _ = state.write_text(json.dumps({"open_for_user": []}))
        closed = self.run_command("questions", "--state", str(state))
        self.assertEqual(closed.returncode, 0, closed.stdout + closed.stderr)
        self.assertEqual(json.loads(outstanding.read_text()), [])
        self.assertEqual(history.read_bytes(), before)
        saved = cast(QuestionRegistration, json.loads(before.decode().strip()))
        self.assertEqual(saved["text"], "Would you retry the build?")
        self.assertIsNotNone(datetime.fromisoformat(saved["time"]).tzinfo)

    def test_questions_rejects_bad_deferred_item_before_writing_outstanding(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"review colors"}]\n', encoding="utf-8")
        before = outstanding.read_bytes()
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "after": "tomorrow"}]}), encoding="utf-8")
        result = self.run_command("questions", "--state", str(state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("questions: failed", result.stdout)
        self.assertEqual(outstanding.read_bytes(), before)


    def test_a_stored_item_with_plumbing_is_named_and_a_new_one_is_refused(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"free the cargo-berth on the Mac"}]\n',
                                   encoding="utf-8")
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": ["free the cargo-berth on the Mac", "review colors"]}),
                             encoding="utf-8")
        kept = self.run_command("questions", "--state", str(state))
        self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
        self.assertIn("questions: reword — free the cargo-berth on the Mac: 'berth' is the production's own plumbing",
                      kept.stdout)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual([entry["text"] for entry in entries], ["free the cargo-berth on the Mac", "review colors"])
        before = outstanding.read_bytes()
        _ = state.write_text(json.dumps({"open_for_user": ["review colors", "release the writers"]}), encoding="utf-8")
        refused = self.run_command("questions", "--state", str(state))
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("questions: failed — release the writers: 'writers' is the production's own plumbing",
                      refused.stdout)
        self.assertEqual(outstanding.read_bytes(), before)

    def test_questions_store_what_waits_on_the_user_and_print_one_line(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"choose layout"}]')
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [
            "review colors", {"text": "choose layout", "after": "2099-01-01T08:00"}]}), encoding="utf-8")
        stored = self.run_command("questions", "--state", str(state))
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        # The reply carries no footer: the user reads the counts in the status line.
        self.assertEqual(stored.stdout.strip(), "questions: ok — recorded")
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual([entry["text"] for entry in entries], ["review colors", "choose layout"])
        self.assertEqual(entries[1]["after"], "2099-01-01T08:00")
        before = outstanding.read_bytes()
        _ = state.write_text(json.dumps({"units": []}), encoding="utf-8")
        kept = self.run_command("questions", "--state", str(state))
        self.assertEqual(kept.stdout.strip(), "questions: ok — recorded")
        self.assertEqual(outstanding.read_bytes(), before)
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "after": "tomorrow"}]}),
                             encoding="utf-8")
        bad = self.run_command("questions", "--state", str(state))
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)
        self.assertIn("questions: failed", bad.stdout)
        self.assertEqual(outstanding.read_bytes(), before)


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


    def test_open_lists_only_the_productions_own_items_in_full_never_those_below(self) -> None:
        for slug in ("example", "kid", "grandkid", "stranger"):
            self.timer(slug)
        self.attach("kid", "example")
        self.attach("grandkid", "kid")
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"},
                            {"since": "2026-10-05T07:00", "text": "review colors", "after": "2099-01-02T08:00"})
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today, or after the release?"})
        self.write_open_for("grandkid", {"since": "2026-09-28T10:30", "text": "keep the old key binding?"})
        self.write_open_for("stranger", {"since": "2026-10-01T08:00", "text": "not this tree's"})
        # The showrunners below answer for their own items (user, 2026-10-27).
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "deferred until Fri Jan 02 08:00: example: review colors",
            "open: ok — 1 due, 1 deferred",
        ])

    def test_open_names_an_unreadable_list(self) -> None:
        self.timer("example")
        _ = self.outstanding().write_text("{bad", encoding="utf-8")
        self.assertEqual(self.open_lines(), [
            "example: needs-user list unreadable",
            "open: ok — 0 due, 0 deferred, 1 unreadable",
        ])

    def test_a_middle_showrunner_lists_its_own_items_and_marks_one_below_never_above(self) -> None:
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
        # `/next all-below` can show the kid's item, so it can be marked from here; the top's never.
        marked = self.run_command("mark-important", "--item", "merge today?")
        self.assertEqual(marked.stdout.strip(), "mark-important: ok — merge today?", marked.stdout + marked.stderr)
        self.assertNotEqual(self.run_command("mark-important", "--item", "ship it?").returncode, 0)

    def test_open_with_nothing_attached_lists_its_own_items_or_none(self) -> None:
        self.assertEqual(self.open_lines(), ["open: ok — 0 due, 0 deferred"])
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"})
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "open: ok — 1 due, 0 deferred",
        ])

    def test_open_and_next_put_the_most_urgent_first_and_keep_the_shown_item(self) -> None:
        self.write_open_for("example", {"since": "2026-10-01T09:00", "text": "old normal"},
                            {"since": "2026-10-06T09:00", "text": "needs the old one", "urgency": "blocks",
                             "follows": "later one"},
                            {"since": "2026-10-02T09:00", "text": "later one", "urgency": "later"},
                            {"since": "2026-10-05T09:00", "text": "by friday", "urgency": "soon", "due": "2026-10-09"})
        self.assertEqual(self.open_lines(), [
            "1. example [later], since Fri Oct 02 09:00: later one",
            "2. example [blocks], since Tue Oct 06 09:00: needs the old one",
            "3. example [soon, due 2026-10-09], since Mon Oct 05 09:00: by friday",
            "4. example, since Thu Oct 01 09:00: old normal",
            "open: ok — 4 due, 0 deferred",
        ])
        shown = self.run_command("next")
        self.assertEqual(shown.stdout.splitlines(), ["example [later], since Fri Oct 02 09:00: later one",
                                                     "next: ok — 3 more due"])
        marked = self.run_command("mark-important", "--item", "old normal")
        self.assertEqual(marked.stdout.strip(), "mark-important: ok — old normal", marked.stdout + marked.stderr)
        # The shown item stays first until it is answered; the user's mark takes the next place.
        lines = self.open_lines()
        self.assertEqual(lines[0], "1. example [later], since Fri Oct 02 09:00: later one")
        self.assertTrue(lines[1].startswith("2. example [user, marked "), lines[1])
        self.write_open_for("example", *cast(list[dict[str, str]], json.loads(self.outstanding().read_text())[:2]))
        self.assertTrue(self.run_command("next").stdout.startswith("example [user, marked "))
        self.assertEqual(self.run_command("mark-important", "--item", "nothing like it").returncode, 2)

    def test_questions_store_a_level_and_keep_the_users_mark(self) -> None:
        outstanding = self.outstanding()
        _ = outstanding.write_text('[{"since":"2026-10-01T08:00","text":"choose layout","urgency":"user",'
                                   + '"marked":"2026-10-01T09:00"},{"since":"2026-10-01T08:00","text":"merge before the demo"}]')
        state = self.root / "judgment.json"
        _ = state.write_text(json.dumps({"open_for_user": [
            {"text": "choose layout", "urgency": "later"},
            {"text": "merge before the demo", "urgency": "soon", "due": "2026-10-09T12:00", "follows": "choose layout"},
        ]}), encoding="utf-8")
        with mock.patch.dict(os.environ, self.env):
            parsed = registration.judgment_state(state, "questions")
        self.assertIsInstance(parsed.outstanding, registration.ReplaceOutstanding)
        stored = self.run_command("questions", "--state", str(state))
        self.assertEqual(stored.returncode, 0, stored.stdout + stored.stderr)
        entries = cast(list[dict[str, str]], json.loads(outstanding.read_text(encoding="utf-8")))
        self.assertEqual({key: entries[0].get(key) for key in ("urgency", "marked")},
                         {"urgency": "user", "marked": "2026-10-01T09:00"})
        self.assertEqual({key: entries[1].get(key) for key in ("urgency", "due", "follows")},
                         {"urgency": "soon", "due": "2026-10-09T12:00", "follows": "choose layout"})
        _ = state.write_text(json.dumps({"open_for_user": [{"text": "choose layout", "urgency": "soon"}]}),
                             encoding="utf-8")
        bad = self.run_command("questions", "--state", str(state))
        self.assertEqual(bad.returncode, 2, bad.stdout + bad.stderr)


    def run_ask(self, text: str, question: str, unit: str = "") -> str:
        with (mock.patch.dict(os.environ, self.env),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state / "notifier"),
              redirect_stdout(io.StringIO()) as output):
            registration.ask(read_production(self.doc), text, question, unit, dailies_render.Ranking())
        return output.getvalue()

    def test_ask_files_a_units_question_with_its_review_and_shows_the_asker(self) -> None:
        full = "Keep the old key binding? Keep (recommended — users know it) or drop it."
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted(full + " Reviewed.")):
            printed = self.run_ask("alpha-unit: keep the old key binding?", full, "alpha-unit")
        self.assertIn("filed question: " + full + " Reviewed.", printed)
        self.assertNotIn("reviewed question:", printed)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual([(row["text"], row["asked_by"], row["question"]) for row in rows],
                         [("alpha-unit: keep the old key binding?", "alpha-unit", full)])
        self.assertEqual(rows[0]["reviewed"], full + " Reviewed.")
        history = self.outstanding().with_suffix(".history.jsonl").read_text()
        self.assertIn("alpha-unit: keep the old key binding?", history)
        listed = self.open_lines()
        self.assertRegex(listed[0], r"^1\. example, since .*, asked by alpha-unit: " + re.escape(full) + "$")

    def test_ask_refuses_an_unknown_unit_a_duplicate_and_a_held_review_without_writing(self) -> None:
        with self.assertRaisesRegex(registration.RegistrationFailure, "beta-unit is not a unit of example"):
            _ = self.run_ask("which?", "Which one?", "beta-unit")
        held = review.QuestionAwaitingFacts("Retry it?", ("say which build",), ())
        with (mock.patch.object(review, "review_question", return_value=held),
              self.assertRaisesRegex(registration.RegistrationFailure, "question held for review: retry\\?: say which build")):
            _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
        self.assertFalse(self.outstanding().exists())
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Retry it?")):
            _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
            with self.assertRaisesRegex(registration.RegistrationFailure, "already open"):
                _ = self.run_ask("retry?", "Retry it?", "alpha-unit")

    def test_a_list_rewrite_keeps_a_filed_question_and_answer_names_the_unit(self) -> None:
        with mock.patch.object(review, "review_question", return_value=review.QuestionAccepted("Retry it?")):
            _ = self.run_ask("retry?", "Retry it?", "alpha-unit")
        state = self.question_state(["Reminder"])
        kept = self.run_questions(state)
        self.assertIn("questions: kept — retry?: asked by alpha-unit", kept)
        rows = cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))
        self.assertEqual([row["text"] for row in rows], ["Reminder", "retry?"])
        self.assertEqual(rows[1]["asked_by"], "alpha-unit")
        # A rewrite that names the item keeps its asker too.
        _ = self.run_questions(self.question_state(["Reminder", "retry?"]))
        self.assertEqual(cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))[1]["asked_by"], "alpha-unit")
        answered = self.run_command("answer", "--item", "retry?")
        self.assertEqual(answered.stdout.splitlines(), ["relay: to alpha-unit", "answer: ok — example: retry?"],
                         answered.stdout + answered.stderr)
        self.assertEqual([row["text"] for row in cast(list[dict[str, str]], json.loads(self.outstanding().read_text()))],
                         ["Reminder"])
        reminder = self.run_command("answer", "--item", "Reminder")
        self.assertEqual(reminder.stdout.splitlines()[0], "relay: none; the item belongs to this production")
        self.assertEqual(self.run_command("answer", "--item", "retry?").returncode, 2)

    def test_answer_reaches_a_list_below_and_relays_through_its_showrunner_never_above(self) -> None:
        for slug in ("top", "example", "kid"):
            self.timer(slug)
        self.attach("example", "top")
        self.attach("kid", "example")
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today?", "question": "Merge today?",
                                    "asked_by": "kid-unit"})
        self.write_open_for("top", {"since": "2026-10-06T07:00", "text": "ship it?", "question": "Ship it?",
                                    "asked_by": "top-unit"})
        answered = self.run_command("answer", "--item", "merge today?")
        self.assertEqual(answered.stdout.splitlines(),
                         ["relay: to the showrunner of kid, for kid-unit", "answer: ok — kid: merge today?"],
                         answered.stdout + answered.stderr)
        self.assertEqual(json.loads(self.outstanding().with_name("kid.json").read_text()), [])
        self.assertEqual(self.run_command("answer", "--item", "ship it?").returncode, 2)

    def test_answer_waits_for_the_lists_lock(self) -> None:
        self.write_open_for("example", {"since": "2026-10-06T08:00", "text": "retry?", "question": "Retry it?",
                                        "asked_by": "alpha-unit"})
        lock = self.outstanding().with_suffix(".lock")
        holder = subprocess.Popen([sys.executable, "-c", (
            "import fcntl, sys, time\n"
            f"handle = open({str(lock)!r}, 'a')\n"
            "fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(2)\n")], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.assertEqual(cast(io.TextIOWrapper, holder.stdout).readline().strip(), "held")
        began = datetime.now()
        answered = self.run_command("answer", "--item", "retry?")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        self.assertGreaterEqual((datetime.now() - began).total_seconds(), 1.0)
        cast(io.TextIOWrapper, holder.stdout).close()

    def test_the_reader_refuses_an_asker_without_a_question(self) -> None:
        _ = self.outstanding().write_text(json.dumps([{"since": "2026-10-06T08:00", "text": "x", "asked_by": "alpha-unit"}]))
        with self.assertRaisesRegex(dailies_render.InputError, "question required"):
            _ = dailies_render.read_outstanding(self.outstanding())

if __name__ == "__main__":
    _ = unittest.main()
