"""Check fresh Claude reviews and the review records."""

from __future__ import annotations

from scripts.shutdown import remote

from scripts.lib import test_hang_guard
from scripts.lib.test_models_registry import ExactRows, registry_text
from scripts.production import staff, unit_config

import json
import shutil
import subprocess
from functools import cached_property
from pathlib import Path
from typing import TypedDict, cast, override

from scripts.direct.recorder_fixture import LauncherFixture, communicate_launcher, configure_launcher_fixture, launcher_environment, prepare_launcher_runtime

from scripts.lint.memory_test_support import config_environment, write_config_fixture


DIRECT_DIR = Path(__file__).parent
AGENTS_DIR = DIRECT_DIR.parent / "agents"


# The wrapper calls this as: <task> readonly <working_dir> <prompt> <findings> <log>.
# The Claude stub can wait, fail, or edit files to test the launcher.
STUB_AGENT_EXEC = """#!/usr/bin/env bash
set -euo pipefail
"${BASH_SOURCE[0]%/*}/../lib/py" - "$@" <<'PYAGENT'
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

_, task, access, working, prompt_file, findings, log = sys.argv
session = Path(os.environ['UNIT_RUN_DIR'])
def stopped(signum, frame):
    if os.environ.get('STUB_REVIEW_DONE_FILE'):
        Path(os.environ['STUB_REVIEW_DONE_FILE']).touch()
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, stopped)
prompt = Path(prompt_file).read_text()
(session / 'agent_prompt_received.txt').write_text(prompt)
if os.environ.get('STUB_AGENT_CALLS'):
    with Path(os.environ['STUB_AGENT_CALLS']).open('a') as calls:
        calls.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('STUB_EXPECT_PREPARED_STYLE'):
    diff_line = next(line for line in prompt.splitlines() if line.startswith('- Diff '))
    (session / 'prepared_at_dispatch.diff').write_bytes(Path(diff_line.split(': ', 1)[1]).read_bytes())
Path(findings).write_text(os.environ.get('STUB_FINDINGS', 'No findings.\\n'))
Path(log).write_text('stub agent log\\n')
if os.environ.get('TEST_WORKER_STARTED'):
    Path(os.environ['TEST_WORKER_STARTED']).touch()
if os.environ.get('STUB_REVIEW_FINISH_FILE'):
    sys.path.insert(0, os.environ['TEST_WAIT_HELPER_DIR'])
    from wait_for_test_state import wait_for_file
    wait_for_file(Path(os.environ['STUB_REVIEW_FINISH_FILE']))
if os.environ.get('STUB_REVIEW_EDIT'):
    Path(os.environ['STUB_REVIEW_EDIT']).write_text('changed by reviewer\\n')
if os.environ.get('STUB_REVIEW_COMMIT'):
    for arguments in (['add', '.'], ['commit', '-m', 'reviewer edit']):
        subprocess.run(['git', '-c', 'user.name=Review Test', '-c',
                        'user.email=review@example.com', *arguments], cwd=working, check=True)
if os.environ.get('STUB_REVIEW_DONE_FILE'):
    Path(os.environ['STUB_REVIEW_DONE_FILE']).touch()
sys.exit(1 if os.environ.get('STUB_TURN_FAILURE') else 0)
PYAGENT
"""


class OpenPass(TypedDict):
    kind: str
    status: str


# "pass" is a keyword, so the recorder's state key needs the functional form.
# The recorder keys the open passes by the team slot that opened each one, so
# the value is a map even when a single launcher is running.
ProgressState = TypedDict("ProgressState", {"pass": dict[str, OpenPass]})


MODELS_REGISTRY = registry_text({
    "assignments": ExactRows({"direct": "claude"}),
    "direct.claude": {"review": "opus:high"},
})


class ReviewLauncherFixture(LauncherFixture):
    """The early-launch contract: before the sentinel, no pass is recorded.

    The reviewer starts while the writer is still working, so the
    implementation pass is open the whole time it runs. A start-pass before the
    sentinel files a review pass for a verdict that cannot have read the final
    diff, and does it under the reviewer's own slot, where nothing later
    reconciles it against the diff it never saw.
    """

    review_script: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.prompt_file: Path = self.root / "review_prompt.md"
        self.review_script = self.root / "scripts" / "direct" / "review.sh"
        _ = unit_config.write(self.working_dir, unit_config.UnitConfig(
            unit_config.PlanFile(Path("plan.md")), unit_config.NoAppPort(), ()))

    @cached_property
    def staff_parent(self) -> str:
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership
        with isolated_staff_file(self.root / "staff.json", {}):
            member = unit_membership("fixture", "fixture")
            return staff.ensure("unit", name=member.name, parent=member.parent,
                                runtime_id=member.current_session,
                                worktree=staff.PresentWorktree(remote.local_machine(), self.working_dir)).current_session

    @override
    def prepare_fixture(self) -> None:
        self.prepare_project(
            "feature/review", "review_prompt.md", "Review the diff.\n",
            (
                "UNIT_PROGRESS_INTERVAL_SECONDS=180",
                "UNIT_COMPACT_ABOVE_TOKENS=100000",
                "ROUTING_FROM_ROUND=4", "ASTRA_AFTER_FAILED_FIXES=2",
                "",
            ), MODELS_REGISTRY,
        )
        _ = write_config_fixture(self.config_file, self.config_file.read_text(encoding="utf-8"))
        _ = config_environment(self.root)
        reviewed = self.working_dir / "reviewed.txt"
        _ = reviewed.write_text("original\n", encoding="utf-8")
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c",
             "user.email=review@example.com", "add", "reviewed.txt"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c",
             "user.email=review@example.com", "commit", "-m", "baseline"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        self.review_script = self.build_script_tree()

    def build_script_tree(self) -> Path:
        """Copy the real wrapper and helpers beside a Claude stub."""
        direct_dir, agents, lib = prepare_launcher_runtime(self.root)
        for name in ("showrunners.py", "session_staff.py", "staff.py", "session_labels.py",
                     "unit_config.py", "unit_names.py"):
            _ = shutil.copy2(DIRECT_DIR.parent / "production" / name,
                             self.root / "scripts/production" / name)
        for directory, names in (("usage", ("agents_log.py",)),
                                 ("herdr", ("place.py", "pane_launch.py")),
                                 ("shutdown", ("remote.py",))):
            destination = self.root / "scripts" / directory
            destination.mkdir()
            for name in names:
                _ = shutil.copy2(DIRECT_DIR.parent / directory / name, destination / name)
        _ = shutil.copy2(DIRECT_DIR.parent / "lib/git_exclude.py", lib / "git_exclude.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib/probe_session.py", lib / "probe_session.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib/process_failure.py", lib / "process_failure.py")
        for name in ("conf.py", "conf.sh", "session_paths.py", "boot_identity.py"):
            _ = shutil.copy2(DIRECT_DIR.parent / "lib" / name, lib / name)
        _ = shutil.copy2(DIRECT_DIR.parent / "production/fast_track.py",
                         self.root / "scripts/production/fast_track.py")
        config = self.root / "config"
        config.mkdir(exist_ok=True)
        _ = shutil.copy2(DIRECT_DIR.parents[1] / "config/fast_track.conf", config / "fast_track.conf")
        for name in ("review.sh", "tag_inventory.py"):
            _ = shutil.copy2(DIRECT_DIR / name, direct_dir / name)
        configure_launcher_fixture(direct_dir / "review.sh")
        # Test the wrapper after admission in this private fixture.
        _ = (direct_dir / "assign_gate.py").write_text("# Admission succeeds in this private fixture.\n")
        lint = self.root / "scripts/lint"
        lint.mkdir()
        for name in ("style_review.py", "lint_config.sh"):
            _ = shutil.copy2(DIRECT_DIR.parent / "lint" / name, lint / name)
        message = self.root / "scripts/message"
        message.mkdir()
        for name in ("sessions.py", "process_identity.py", "relay_identity.py"):
            _ = shutil.copy2(DIRECT_DIR.parent / "message" / name, message / name)
        review_config = self.root / "config/review"
        review_config.mkdir(parents=True)
        _ = shutil.copy2(DIRECT_DIR.parents[1] / "config/review/style.md", review_config / "style.md")
        stub = agents / "agent_exec.sh"
        _ = stub.write_text(STUB_AGENT_EXEC, encoding="utf-8")
        stub.chmod(0o755)
        return direct_dir / "review.sh"

    @override
    def environment(self) -> dict[str, str]:
        environment = launcher_environment(self.root)
        environment["STAFF_FILE"] = str(self.root / "staff.json")
        environment["CODEX_HOME"] = str(self.root / "codex-home")
        environment["CODEX_THREAD_ID"] = self.staff_parent
        environment["PYTHONPATH"] = str(DIRECT_DIR.resolve().parents[1])
        _ = environment.pop("STAFF_ID", None)
        environment["LINT_CONFIG_FILE"] = str(self.root / "lint.conf")
        environment["UNIT_EARLY_POLL_SECS"] = "0.01"
        environment["TEST_WAIT_HELPER_DIR"] = str(DIRECT_DIR.resolve())
        environment["FAST_TRACK_STATE_DIR"] = str(self.root / "fast-track-state")
        environment["CONF_DIR"] = str(self.root)
        return environment

    def checked_review(
        self, session: Path, *options: str, lens: str = "", early: str = "",
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        _ = environment.pop("TEST_LEGACY_LAUNCH", None)
        environment["TEST_WORKER_STARTED"] = str(session / "review-worker-started")
        return subprocess.run(
            ["bash", str(self.review_script), *options, str(session), str(self.working_dir),
             str(self.prompt_file), "review", "blind review", "reviewing the diff", "1", lens, early],
            check=False, capture_output=True, text=True, env=environment, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def finish_early_launcher(self, process: subprocess.Popen[str], ready: Path,
                              finish: Path, timeout: float = test_hang_guard.TEST_HANG_GUARD_SECS) -> tuple[str, str]:
        ready.touch()
        finish.touch()
        return communicate_launcher(process, timeout)

    @override
    def recorder(self, *arguments: str) -> None:
        if arguments[0] == "start-run":
            values = list(arguments)
            values[values.index("--main-session-id") + 1] = self.staff_parent
            arguments = tuple(values)
        super().recorder(*arguments)

    def start_implementation(self, name: str) -> Path:
        session_dir = self.start_phase(name)
        self.recorder(
            "start-pass", "--session-dir", str(session_dir), "--phase-id", "3",
            "--pass-kind", "impl", "--activity", "writing the retry path",
            "--called-task", "direct.impl", "--called-family", "codex",
            "--called-model", "gpt-called", "--called-effort", "xhigh",
        )
        return session_dir

    def review_command(
        self, session_dir: Path, lens: str = "contract", ready_file: str = "",
    ) -> list[str]:
        command = [
            "bash", str(self.review_script), str(session_dir), str(self.working_dir),
            str(self.prompt_file), "review", "blind review", "reviewing the diff",
            "1", lens, ready_file,
        ]
        return command

    def run_review(self, session_dir: Path, ready_file: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.review_command(session_dir, "", str(ready_file)),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def run_claude_review(
        self,
        session_dir: Path,
        *,
        edit_path: str = "",
        commit_change: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        environment["STUB_REVIEW_WORKING_DIR"] = str(self.working_dir)
        if edit_path:
            environment["STUB_REVIEW_EDIT"] = edit_path
        if commit_change:
            environment["STUB_REVIEW_COMMIT"] = "1"
        return subprocess.run(
            self.review_command(session_dir, "", ""),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def finish_implementation(self, session_dir: Path) -> None:
        self.recorder(
            "finish-pass", "--session-dir", str(session_dir), "--status", "completed"
        )

    def events(self, name: str) -> list[dict[str, object]]:
        path = self.history_dir / "runs" / f"{name}.jsonl"
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def pass_events(self, name: str) -> list[dict[str, object]]:
        return [
            event
            for event in self.events(name)
            if str(event.get("event_type", "")).startswith("pass_")
        ]


    def turn_records(self, session_dir: Path) -> list[dict[str, object]]:
        directory = self.history_dir / "review_turns" / session_dir.name
        return [cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(directory.glob("*.json"))]

    def call_records(self, filename: str) -> list[dict[str, object]]:
        return [cast("dict[str, object]", json.loads(line)) for line in
                (self.root / filename).read_text(encoding="utf-8").splitlines()]

    def trace_pass_recorder(self, environment: dict[str, str]) -> None:
        recorder = self.review_script.parent / "progress_history.py"
        _ = recorder.rename(recorder.with_name("progress_history_real.py"))
        _ = recorder.write_text('''import json
import os
from pathlib import Path
import sys

if sys.argv[1] in ("start-pass", "finish-pass"):
    with Path(os.environ["STUB_PASS_CALLS"]).open("a") as calls:
        calls.write(json.dumps({"verb": sys.argv[1], "argv": sys.argv[2:]}) + "\\n")
os.execv(sys.executable, [sys.executable, str(Path(__file__).with_name("progress_history_real.py")), *sys.argv[1:]])
''', encoding="utf-8")
        calls = self.root / "pass-calls.jsonl"
        calls.touch()
        environment["STUB_PASS_CALLS"] = str(calls)

    def side_by_side_review(self, name: str, edited: str, *, commit_change: bool = False) -> tuple[
            subprocess.CompletedProcess[str], Path]:
        """Review phase 3 while phase 4, open beside it, holds `other.txt` and its writer edits `edited`."""
        session = self.start_implementation(name)
        self.finish_implementation(session)
        # The stub's commit adds every file, so the plan start_implementation writes is committed first.
        _ = subprocess.run(["git", "add", "plan.md"], cwd=self.working_dir, check=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c", "user.email=review@example.com",
             "commit", "-q", "-m", "plan"],
            cwd=self.working_dir, check=True, capture_output=True, text=True,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )
        for phase, files in (("3", "reviewed.txt\n"), ("4", "other.txt\n")):
            directory = session / "phases" / phase
            directory.mkdir(parents=True, exist_ok=True)
            _ = (directory / "delegated_phase_reservation_state.json").write_text(
                json.dumps({"state": "active", "phase": phase}), encoding="utf-8")
            _ = (directory / "files").write_text(files, encoding="utf-8")
        # A phase is open while the recorder lists it, so phase 4 starts there beside phase 3.
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Parallel writer")
        environment = self.environment()
        environment["STUB_REVIEW_EDIT"] = str(self.working_dir / edited)
        environment["STUB_REVIEW_WORKING_DIR"] = str(self.working_dir)
        if commit_change:
            environment["STUB_REVIEW_COMMIT"] = "1"
        command = self.review_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        return result, session / "phases" / "3"
