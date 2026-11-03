"""Check the Claude simplify command without starting a model."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import shlex
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.whoami.account_test_support import write_claude_account
from scripts.direct.recorder_fixture import communicate_launcher, invoke_recorder
from scripts.direct.wait_for_test_state import wait_for_file, wait_until
from scripts.lib.nix_env_test_support import NixFixture, ROOT
from scripts.lib.git_test_support import commit_repository
from scripts.lib.test_agents_registry import write_registry
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production.staff_test_support import fixture_runtime_id


class SimplifyDispatchTests(NixFixture):
    session_dir: Path = Path()
    diff: Path = Path()
    prompt: Path = Path()
    calls: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        shim = self.home / ".claude/scripts/lib/py"
        shim.parent.mkdir(parents=True)
        shim.symlink_to(ROOT / "scripts/lib/py")
        _ = (self.home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "fixture@example.invalid"},
        }))
        registry = write_registry(self.root / "agents.conf", base_text=(ROOT / "config/agents.conf").read_text())
        self.env.update({"AGENTS_CONFIG_FILE": str(registry), "AGENTS_CONFIG_PUBLISH": "0",
                         "CODEX_CATALOG_SYNC_STATE_FILE": str(registry), "USAGE_STATE_DIR": str(self.root / "usage")})
        self.env.update(config_environment(self.root))
        # The fixture sets the lens itself; the live config may switch it off.
        _ = write_config_fixture(self.root / "direct.conf", "SIMPLIFY_LENS=on\n")
        self.env.update({"UNIT_HISTORY_DIR": str(self.root / "history"),
                         "UNIT_ACTIVE_DIR": str(self.root / "active"),
                         "UNIT_SYNC_WRITES": "0", "PYTHONPATH": str(ROOT),
                         "CODEX_THREAD_ID": fixture_runtime_id("simplify-director"),
                         "TZ": "UTC", "USER_ZONE": "UTC"})
        self.env["AGENT_NOTES_DIR"] = str(self.root / "notes")
        self.claude_quota("claude1", 100)
        _ = subprocess.run(["git", "init", "-b", "simplify-fixture", str(self.work)],
                           env=self.env, capture_output=True, text=True,
                           check=True, timeout=TEST_HANG_GUARD_SECS)
        _ = commit_repository(self.work, "Empty fixture", allow_empty=True)
        standing = self.root / "review/simplify.md"
        standing.parent.mkdir()
        _ = standing.write_text("Report only. Edit no file.\nReport each item with its file, line, change, and reason.\nStanding user words.\n")
        self.session_dir = self.root / "run"
        result = invoke_recorder(
            ["start-run", "--session-dir", str(self.session_dir), "--working-dir", str(self.work),
             "--main-family", "codex", "--main-model", "fixture-model", "--main-effort", "high",
             "--main-session-id", self.env["CODEX_THREAD_ID"]], self.env, self.work,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.diff = self.root / "phase.diff"
        _ = self.diff.write_text("diff --git a/worker.py b/worker.py\n-old\n+new\n")
        self.prompt = self.root / "source.md"
        _ = self.prompt.write_text("Review the saved phase diff.\nUnit user words.\n")
        self.calls = self.root / "claude_calls.jsonl"
        self.env["SIMPLIFY_TEST_CALLS"] = str(self.calls)
        self.script(self.bin / "claude", '''import json, os, signal, sys
from pathlib import Path
def stopped(signum, frame):
    Path(os.environ["SIMPLIFY_TEST_CALLS"] + ".stopped").touch()
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stopped)
with Path(os.environ["SIMPLIFY_TEST_CALLS"]).open("a") as stream:
    stream.write(json.dumps({"argv": sys.argv[1:], "prompt": sys.stdin.read(), "pid": os.getpid(),
                             "background_disabled": os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
                             "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\\n")
if os.environ.get("SIMPLIFY_TEST_STARTED"):
    Path(os.environ["SIMPLIFY_TEST_STARTED"]).touch()
if os.environ.get("SIMPLIFY_TEST_GATE"):
    with Path(os.environ["SIMPLIFY_TEST_GATE"]).open() as gate:
        gate.read()
if os.environ.get("SIMPLIFY_TEST_EDIT"):
    Path(os.environ["SIMPLIFY_TEST_EDIT"]).write_text("Changed by reviewer")
for event in json.loads(os.environ.get("SIMPLIFY_TEST_EVENTS", '[{"type": "result", "result": "Pass"}]')):
    print(json.dumps(event))
raise SystemExit(int(os.environ.get("SIMPLIFY_TEST_EXIT", "0")))
''')

    def claude_quota(self, label: str, left: float, *, reset: datetime | None = None) -> None:
        reset = reset if reset is not None else datetime.now(timezone.utc) + timedelta(days=2)
        with patch.dict(os.environ, self.env):
            write_claude_account(label, f"{label}@example.invalid", left, reset)

    def command(self, *options: str) -> list[str]:
        return ["bash", str(ROOT / "scripts/direct/assign.sh"), "--role", "review", "--lens", "simplify",
                "--session-dir", str(self.session_dir), "--phase", "simplify", "--cwd", str(self.work),
                "--prompt", str(self.prompt), "--diff", str(self.diff), *options]

    def launch(self, *options: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(self.command(*options), env=self.env, capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def stop_review(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
        _ = communicate_launcher(process)

    def stop_orphan_review(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def start_blocked_review(self, phase: str = "simplify") -> tuple[subprocess.Popen[str], Path]:
        gate = self.root / f"{phase}.gate"
        os.mkfifo(gate)
        started = self.root / f"{phase}.started"
        environment = {**self.env, "SIMPLIFY_TEST_GATE": str(gate), "SIMPLIFY_TEST_STARTED": str(started)}
        process = subprocess.Popen(self.command("--phase", phase), env=environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.stop_review, process)
        wait_until(started.exists, started.parent, processes=(process,))
        return process, gate

    def records(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(path.read_text())) for path in self.records_dir().glob("*.json")]

    def records_dir(self) -> Path:
        return Path(self.env["UNIT_HISTORY_DIR"]) / "review_turns" / self.session_dir.name

    def status(self) -> str:
        return self.phase_file("review_status_simplify").read_text().splitlines()[0]

    def passes(self) -> list[dict[str, object]]:
        state = cast(dict[str, object], json.loads((self.session_dir / "progress_history_state.json").read_text()))
        return list(cast(dict[str, dict[str, object]], state["pass"]).values())

    def claude_calls(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in self.calls.read_text().splitlines()]

    def phase_file(self, name: str) -> Path:
        return self.session_dir / "phases/simplify" / name

    def test_a_missing_diff_records_error_without_starting_claude(self) -> None:
        self.diff.unlink()
        summary = self.root / "missing-diff-summary.txt"

        result = self.launch("--summary", str(summary))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.status(), "error")
        self.assertIn("simplify: error: unable to prepare the simplify review:", result.stderr)
        self.assertIn(str(self.diff), result.stderr)
        report = self.phase_file("review_findings_1_simplify.txt").read_text()
        self.assertIn("No such file or directory", report)
        self.assertIn(str(self.diff), report)
        self.assertEqual(summary.read_text(), report)
        self.assertFalse(self.calls.exists())

    def test_a_preparation_failure_replaces_the_previous_report_and_requested_summary(self) -> None:
        summary = self.root / "summary.txt"
        _ = self.launch("--summary", str(summary))
        self.assertEqual(summary.read_text(), "Pass")
        self.diff.unlink()

        result = self.launch("--summary", str(summary))

        self.assertEqual(self.status(), "error")
        report = self.phase_file("review_findings_1_simplify.txt").read_text()
        self.assertIn("No such file or directory", report)
        self.assertEqual(summary.read_text(), report)
        self.assertNotIn("Pass", report)
        self.assertIn(str(self.diff), result.stderr)
        self.assertEqual(len(self.claude_calls()), 1)

    def test_helper_failures_during_preparation_and_review_publish_error_without_python(self) -> None:
        for command, previous in (("prepare", "preparing\n"), ("check", "reviewing\n")):
            with self.subTest(command=command):
                snapshot = self.root / f"{command}.status"
                python = self.bin / "failed-helper-python"
                self.script(python, f'''import os, sys
from pathlib import Path
if len(sys.argv) > 2 and sys.argv[1].endswith("/simplify_result.py"):
    if sys.argv[2] == {command!r}:
        Path({str(snapshot)!r}).write_text(Path({str(self.phase_file("review_status_simplify"))!r}).read_text())
    if Path({str(snapshot)!r}).exists():
        print("result helper unavailable", file=sys.stderr)
        raise SystemExit(9)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
                self.env["CLAUDE_PYTHON"] = str(python)

                result = self.launch()

                self.assertEqual(snapshot.read_text(), previous)
                self.assertEqual(self.phase_file("review_status_simplify").read_text(), "error\n")
                self.assertIn("result helper unavailable", result.stderr)
                self.assertIn("ERROR: unable to finish the simplify result.", result.stderr)
                self.assertIn("simplify: error; summary", result.stdout)
                self.assertEqual(result.returncode, 0)

    def test_a_prompt_source_at_the_generated_path_stays_separate_on_each_launch(self) -> None:
        source = self.phase_file("review_prompt_1_simplify.md")
        source.parent.mkdir(parents=True)
        caller_words = "Work Order: Preserve the caller instructions."
        _ = source.write_text(caller_words + "\n")
        original = source.read_bytes()
        for _ in range(2):
            result = self.launch("--prompt", str(source))

            self.assertEqual(self.status(), "reviewed", result.stderr)
            self.assertEqual(source.read_bytes(), original)
            generated = str(self.claude_calls()[-1]["prompt"])
            self.assertEqual(sum(line.startswith("/simplify ") for line in generated.splitlines()), 1)
            self.assertEqual(generated.count(caller_words), 1)
            self.assertEqual(generated.count("Standing user words."), 1)

    def test_same_diff_starts_a_fresh_review_and_replaces_the_report(self) -> None:
        copy = self.root / "review-copy.txt"
        _ = self.launch("--summary", str(copy))
        self.assertEqual(self.status(), "reviewed")
        generated = self.phase_file("review_prompt_1_simplify.md").read_text()
        self.assertEqual(shlex.split(generated.splitlines()[0]), ["/simplify", str(self.diff)])
        for words in ("Report only. Edit no file.", "Standing user words.", "Unit user words."):
            self.assertIn(words, generated)
        self.assertEqual(self.claude_calls()[0]["prompt"], generated)
        summary = self.phase_file("review_findings_1_simplify.txt")
        original = summary.read_text()
        _ = summary.write_text("lost current report")

        self.assertEqual(copy.read_text(), original)
        _ = copy.write_text("lost copy")
        second = self.launch("--summary", str(copy))

        self.assertEqual(copy.read_text(), original)
        self.assertIn("simplify: reviewed; summary", second.stdout)
        self.assertEqual(len(self.claude_calls()), 2)
        self.assertEqual(summary.read_text(), original)
        self.assertFalse((self.session_dir / "mesh_roster.json").exists())
        _ = self.diff.write_text(self.diff.read_text() + "+another change\n")
        _ = self.launch()
        self.assertEqual(len(self.claude_calls()), 3)

    def test_off_switch_is_read_again_after_a_successful_launch(self) -> None:
        _ = self.launch()
        _ = self.diff.write_text(self.diff.read_text() + "+another change\n")
        _ = write_config_fixture(self.root / "direct.conf", "SIMPLIFY_LENS=off\n")

        disabled = self.launch()

        self.assertIn("off", disabled.stdout)
        self.assertEqual(len(self.claude_calls()), 1)
        _ = write_config_fixture(self.root / "direct.conf", "SIMPLIFY_LENS=on\n")
        _ = self.launch()
        self.assertEqual(len(self.claude_calls()), 2)

    def test_failed_claude_run_reports_error_and_allows_the_phase_to_continue(self) -> None:
        self.env["SIMPLIFY_TEST_EXIT"] = "9"
        result = self.launch()
        self.assertIn("error", result.stdout + result.stderr)
        self.assertEqual(self.status(), "error")
        self.assertEqual(len(self.claude_calls()), 1)

    def events(self, *events: dict[str, object]) -> None:
        self.env["SIMPLIFY_TEST_EVENTS"] = json.dumps(events)

    def execution_failure(self, reason: str) -> None:
        self.events({
            "type": "result", "is_error": True, "subtype": "error_during_execution",
            "errors": [reason], "result": "",
        })

    def test_execution_errors_keep_the_errors_array_reason_once(self) -> None:
        reason = "Agent startup failed: credentials expired"
        self.execution_failure(reason)

        result = self.launch()

        self.assertEqual(self.status(), "error")
        self.assertEqual([line for line in result.stderr.splitlines() if reason in line],
                         [f"simplify: error: {reason}; error_during_execution"])
        self.assertIn(reason, self.phase_file("review_findings_1_simplify.txt").read_text())
        self.assertEqual(len(self.claude_calls()), 1)

    def test_a_failed_review_runs_again_on_the_same_diff(self) -> None:
        self.execution_failure("limit reached")
        _ = self.launch()

        _ = self.launch()
        self.assertEqual(len(self.claude_calls()), 2)
        self.assertEqual(self.status(), "error")
        self.events({"type": "result", "result": "Pass"})
        _ = self.launch()
        self.assertEqual(self.status(), "reviewed")
        self.assertEqual(len(self.claude_calls()), 3)

    def test_no_account_failure_runs_again_without_starting_claude(self) -> None:
        reset = datetime.now(timezone.utc) + timedelta(days=2)
        self.claude_quota("claude1", 0, reset=reset)
        result = self.launch()
        self.assertIn("no claude account has room; the earliest reset is", result.stderr)
        self.assertIn(f"{reset:%a %Y-%m-%d %H:%M} UTC", result.stderr)

        _ = self.launch()

        self.assertEqual(len(self.records()), 2)
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.status(), "error")

    def test_simplify_uses_foreground_agents_and_retains_each_agents_result(self) -> None:
        _ = (self.root / "review/simplify.md").write_text((ROOT / "config/review/simplify.md").read_text())
        report = "Pass\n\nAgent results\nreuse: Pass\nsimplification: Pass\nefficiency: Pass\naltitude: Pass"
        self.events({
            "type": "result", "is_error": False, "result": report,
            "subagent_stats": {"spawned": 4, "completed": 4, "failed": 0},
        })

        _ = self.launch()

        call, = self.claude_calls()
        self.assertEqual(call["background_disabled"], "1")
        self.assertIn("Wait for all listed agents to return their results", str(call["prompt"]))
        self.assertEqual(self.status(), "reviewed")
        self.assertEqual(self.phase_file("review_findings_1_simplify.txt").read_text(), report)

    def test_an_is_error_result_cannot_be_recorded_as_reviewed(self) -> None:
        self.events({"type": "result", "is_error": True, "result": "Pass"})

        result = self.launch()

        self.assertEqual(self.status(), "error")
        self.assertIn("simplify: error: Pass", result.stderr)
        record, = self.records()
        self.assertEqual(record["status"], "error")

    def test_the_simplify_launch_records_the_selected_claude_account(self) -> None:
        self.claude_quota("claude2", 0)
        self.env["CLAUDE_CONFIG_DIR"] = str(self.home / ".claude2")

        result = self.launch()

        self.assertIn("Claude account claude1", result.stderr)
        self.assertIn("Claude account claude1", self.phase_file("review_agent_1_simplify.log").read_text())
        self.assertEqual(self.claude_calls()[0]["config_dir"], str(self.home / ".claude1"))
        self.assertEqual(self.status(), "reviewed")

    def test_busy_lens_reports_error_without_starting_claude(self) -> None:
        self.phase_file(".").mkdir(parents=True)
        with self.phase_file("review_launch_simplify.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.launch()
        self.assertIn("busy", result.stderr)
        self.assertFalse(self.phase_file("review_status_simplify").exists())
        self.assertFalse(self.phase_file("review_findings_1_simplify.txt").exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.phase_file("review_prompt_1_simplify.md").exists())

    def test_a_rejected_launch_preserves_the_running_owners_report_status_and_summary(self) -> None:
        gate = self.root / "validation.gate"
        os.mkfifo(gate)
        started = self.root / "validation.started"
        python = self.bin / "validation-python"
        self.script(python, f'''import os, sys
from pathlib import Path
if len(sys.argv) > 2 and sys.argv[1].endswith("/simplify_result.py") and sys.argv[2] == "check":
    Path({str(started)!r}).touch()
    with Path({str(gate)!r}).open() as stream:
        stream.read()
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        summary = self.root / "shared-summary.txt"
        process = subprocess.Popen(self.command("--summary", str(summary)),
                                   env={**self.env, "CLAUDE_PYTHON": str(python)}, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.stop_review, process)
        released = False
        try:
            wait_until(started.exists, started.parent, processes=(process,))
            report = self.phase_file("review_findings_1_simplify.txt")
            self.assertEqual(report.read_text(), "Pass")
            paths = (report, self.phase_file("review_status_simplify"), summary)
            before = {path: path.read_bytes() if path.exists() else None for path in paths}

            rejected = self.launch("--summary", str(summary))

            self.assertIn("busy", rejected.stderr)
            self.assertEqual({path: path.read_bytes() if path.exists() else None for path in paths}, before)
            self.assertEqual(len(self.claude_calls()), 1)
            _ = gate.write_text("finish")
            released = True
            _ = communicate_launcher(process)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(self.status(), "reviewed")
            self.assertEqual(report.read_text(), "Pass")
            self.assertEqual(summary.read_text(), "Pass")
            record, = self.records()
            self.assertEqual(record["status"], "reviewed")
        finally:
            if started.exists() and not released:
                _ = gate.write_text("finish")

    def test_closure_review_starts_no_claude_run(self) -> None:
        _ = self.launch("--pass", "2")
        self.assertFalse(self.calls.exists())

    def test_generated_prompt_preserves_its_source_and_reads_the_standing_words_once(self) -> None:
        _ = self.prompt.write_text("Review the user's helper.\nUnit user words.\n")
        source = self.prompt.read_bytes()
        for extra in ("", "+another change\n"):
            _ = self.diff.write_text(self.diff.read_text() + extra)
            _ = self.launch("--summary", str(self.root / "requested-summary"))
            generated = self.phase_file("review_prompt_1_simplify.md").read_text()
            self.assertEqual(generated.count("Report only. Edit no file."), 1)
            self.assertEqual(generated.count("Standing user words."), 1)
            self.assertEqual(self.prompt.read_bytes(), source)
            self.assertEqual((self.root / "requested-summary").read_text(), "Pass")

    def test_invalid_standing_templates_record_a_preparation_error(self) -> None:
        for template in ("Keep $missing agents", "Keep ${agents"):
            with self.subTest(template=template):
                _ = (self.root / "review/simplify.md").write_text(template)
                result = self.launch()
                self.assertEqual(self.status(), "error")
                self.assertIn("unable to prepare the simplify review", result.stderr)
                self.assertFalse(self.calls.exists())

    def test_simplify_requires_an_explicit_diff_even_when_the_prompt_names_one(self) -> None:
        _ = self.prompt.write_text(f"/simplify {self.diff}\n")
        result = subprocess.run(self.command("--diff", ""), env=self.env, capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--diff is required", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_the_shared_launcher_refuses_a_codex_assignment_for_simplify(self) -> None:
        registry = Path(self.env["AGENTS_CONFIG_FILE"])
        _ = write_registry(registry, {"assignments": {"direct.simplify": "codex"},
                                      "direct.codex": {"simplify": "sol:high"}}, base_text=registry.read_text())

        result = self.launch()

        self.assertIn("direct.simplify requires a Claude assignment", result.stderr)
        self.assertEqual(self.status(), "error")
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.passes(), [])

    def test_the_shared_launcher_records_the_simplify_task_and_claude_assignment(self) -> None:
        _ = self.launch()
        arguments = cast(list[str], self.claude_calls()[0]["argv"])
        session_id = arguments[arguments.index("--session-id") + 1]
        assignments = [cast(dict[str, object], json.loads(line)) for line in
                       (Path(self.env["USAGE_STATE_DIR"]) / "agents.jsonl").read_text().splitlines()]
        self.assertTrue(any(row.get("id") == session_id and row.get("role") == "review"
                            and row.get("lens") == "simplify" for row in assignments))
        record, = self.records()
        self.assertEqual((record["lens"], record["thread_id"], record["status"]),
                         ("simplify", session_id, "reviewed"))
        recorded_pass, = self.passes()
        self.assertEqual((recorded_pass["called_task"], recorded_pass["status"]), ("direct.simplify", "completed"))
        self.assertIn("simplify", (self.session_dir / "heartbeat.log").read_text())
        self.assertEqual(str(uuid.UUID(session_id)), session_id)
        self.assertEqual(arguments.count("--session-id"), 1)
        self.assertEqual(list(self.phase_file("").glob("*.session")), [])

    def test_a_failed_assignment_append_does_not_stop_the_review(self) -> None:
        shim = self.bin / "review-python"
        self.script(shim, 'import os, sys\n'
                    + 'if sys.argv[1].endswith("/usage/agents_log.py") and sys.argv[2] == "append":\n'
                    + '    print("Assignment append failed", file=sys.stderr)\n'
                    + '    raise SystemExit(9)\n'
                    + f'os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n')
        self.env["CLAUDE_PYTHON"] = str(shim)

        result = self.launch()

        self.assertIn("Assignment append failed", result.stderr)
        self.assertEqual(self.status(), "reviewed")
        self.assertEqual(len(self.claude_calls()), 1)
        record, = self.records()
        self.assertEqual(record["status"], "reviewed")
        self.assertTrue(record["thread_id"])

    def test_report_validation_trims_whitespace_and_requires_pass_or_a_numbered_item(self) -> None:
        for report, outcome, raised in ((" \nPass\t\n", "reviewed", 0),
                                        ("\u00a0Pass\u00a0", "reviewed", 0),
                                        (" \n1. Remove the duplicate.\n", "reviewed", 1),
                                        ("1.\u00a0Remove the duplicate.", "reviewed", 1),
                                        ("\t1) Remove the duplicate.  ", "reviewed", 1),
                                        ("1.Missing space", "error", 0),
                                        ("2. Wrong first number", "error", 1),
                                        ("", "error", 0)):
            with self.subTest(report=report):
                self.events({"type": "result", "result": report})
                _ = self.diff.write_text(self.diff.read_text() + "+change\n")

                result = self.launch()
                if not report:
                    self.assertIn("empty simplify report", result.stderr)

                self.assertEqual(self.status(), outcome)
                records = sorted(self.records(), key=lambda record: float(str(record["finished_at"])))
                self.assertEqual((records[-1]["status"], records[-1]["raised"]), (outcome, raised))

    def test_an_ad_hoc_review_uses_the_shared_phase_directory(self) -> None:
        _ = self.launch("--phase", "ad hoc")

        directory = self.session_dir / "phases/adhoc"
        self.assertEqual((directory / "review_status_simplify").read_text().splitlines()[0], "reviewed")
        self.assertEqual((directory / "review_prompt_1_simplify.md").read_text().splitlines()[0],
                         f"/simplify {self.diff}")
        self.assertFalse((self.session_dir / "phases/ad hoc").exists())

    def test_relative_and_home_diff_paths_resolve_to_the_saved_diff(self) -> None:
        diff = self.home / "saved.diff"
        _ = diff.write_text(self.diff.read_text())
        for path in ("../../saved.diff", "~/saved.diff"):
            with self.subTest(path=path):
                _ = diff.write_text(diff.read_text() + "+change\n")

                _ = self.launch("--diff", path)

                generated = self.phase_file("review_prompt_1_simplify.md").read_text()
                self.assertEqual(shlex.split(generated.splitlines()[0]), ["/simplify", str(diff)])

    def test_stopping_the_launcher_stops_claude_and_closes_its_pass(self) -> None:
        process, _ = self.start_blocked_review()
        self.assertEqual(int(self.phase_file("review_pid_simplify").read_text()), process.pid)
        process.terminate()
        _ = communicate_launcher(process)
        self.assertEqual(process.returncode, 143)
        self.assertEqual(self.status(), "error")
        wait_for_file(Path(str(self.calls) + ".stopped"))
        recorded_pass, = self.passes()
        self.assertEqual(recorded_pass["status"], "error")
        _ = self.launch()
        self.assertEqual(len(self.claude_calls()), 2)

    def test_a_live_child_keeps_the_lock_after_the_review_process_is_killed(self) -> None:
        process, gate = self.start_blocked_review()
        child_pid = cast(int, self.claude_calls()[0]["pid"])
        self.addCleanup(self.stop_orphan_review, child_pid)
        process.kill()
        _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        retry = self.launch()
        self.assertIn("busy", retry.stderr)
        self.assertEqual(len(self.claude_calls()), 1)
        _ = gate.write_text("finish")
        _ = communicate_launcher(process)

        _ = self.launch()

        self.assertEqual(len(self.claude_calls()), 2)
        self.assertEqual(self.status(), "reviewed")

    def test_concurrent_phases_keep_separate_passes_when_the_first_review_finishes(self) -> None:
        first, first_gate = self.start_blocked_review()
        result = invoke_recorder(["start-phase", "--session-dir", str(self.session_dir), "--phase-id", "second",
                                  "--phase-title", "Second phase"], self.env, self.work)
        self.assertEqual(result.returncode, 0, result.stderr)
        second, second_gate = self.start_blocked_review("second")
        _ = first_gate.write_text("finish")
        first_output = communicate_launcher(first)
        self.assertEqual(first.returncode, 0, str(first_output))
        passes = {cast(dict[str, object], record["phase"])["id"]: record for record in self.passes()}
        self.assertEqual(passes["simplify"]["status"], "completed")
        self.assertEqual(passes["second"]["status"], "active")
        _ = second_gate.write_text("finish")
        second_output = communicate_launcher(second)
        self.assertEqual(second.returncode, 0, str(second_output))
        self.assertTrue(all(record["status"] == "completed" for record in self.passes()))

    def test_the_shared_tree_guard_rejects_a_claude_edit(self) -> None:
        self.env["SIMPLIFY_TEST_EDIT"] = str(self.work / "unexpected.txt")
        result = self.launch()
        self.assertEqual(self.status(), "error")
        record, = self.records()
        self.assertEqual(record["status"], "error")
        self.assertIn("simplify reviewer changed the working tree", result.stderr)
        self.assertNotIn("review worker  changed", result.stderr)
