#!/usr/bin/env python3
"""Integration tests for review.sh's fresh and long-lived reviewer paths."""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import TypedDict, cast, override

from scripts.direct import wait_for_test_state
from scripts.direct.local_release_wait import IntervalReleasePolling
from scripts.direct.wait_for_test_state import wait_for_file, wait_for_pid, wait_until
from scripts.direct.recorder_fixture import communicate_launcher, invoke_recorder


DIRECT_DIR = Path(__file__).parent
AGENTS_DIR = DIRECT_DIR.parent / "agents"

LEGACY_RECORDER_ARGUMENTS = """
# Compatibility for older launch cases whose subject is root-level artifacts.
# The new launch-boundary tests remove this flag and use the recorder unchanged.
import json as _fixture_json
import os as _fixture_os
from pathlib import Path as _FixturePath
import sys as _fixture_sys
if (_fixture_os.environ.get("TEST_LEGACY_LAUNCH") == "1"
        and len(_fixture_sys.argv) > 1 and _fixture_sys.argv[1] in ("start-pass", "check-launch")):
    if "--phase-id" not in _fixture_sys.argv:
        _fixture_session = _FixturePath(
            _fixture_sys.argv[_fixture_sys.argv.index("--session-dir") + 1])
        _fixture_state = _fixture_json.loads(
            (_fixture_session / "progress_history_state.json").read_text())
        _fixture_sys.argv.extend(["--phase-id", _fixture_state["phase"]["id"]])

"""

# The wrapper calls this as: <task> readonly <working_dir> <prompt> <findings> <log>.
# Writing both outputs and exiting immediately drives the wrapper's completion
# branch without a model call.
STUB_AGENT_EXEC = """#!/usr/bin/env bash
set -euo pipefail
printf 'No findings.\\n' > "$5"
printf 'stub agent log\\n' > "$6"
if [[ -n "${TEST_WORKER_STARTED:-}" ]]; then : > "$TEST_WORKER_STARTED"; fi
"""

STUB_MESH = """#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import time

verb = sys.argv[1]

def option(name):
    return sys.argv[sys.argv.index(name) + 1]

session = pathlib.Path(option('--session-dir'))
roster_path = session / 'mesh_roster.json'
roster = json.loads(roster_path.read_text()) if roster_path.exists() else {}
if os.environ.get('STUB_MESH_CALLS'):
    with pathlib.Path(os.environ['STUB_MESH_CALLS']).open('a') as calls:
        calls.write(json.dumps(sys.argv[1:]) + '\\n')
if verb in ('start', 'compact', 'can-follow', 'follow'):
    with (session / 'mesh_events.txt').open('a') as events:
        events.write(verb + '\\n')

name = option('--name') if verb == 'start' else option('--to')
entry = roster.get(name, {})
if verb == 'start':
    entry = {
        'thread_id': 'thread-' + name,
        'turn_id': 'turn-1',
        'status': 'running',
        'role': option('--role'),
        'task': option('--task'),
        'context_tokens': 12000,
    }
    (session / 'follow_message.txt').write_text(
        pathlib.Path(option('--prompt-file')).read_text()
    )
    pathlib.Path(option('--summary-file')).write_text('No findings.\\n')
    pathlib.Path(option('--log-file')).write_text('stub mesh log\\n')
    entry['status'] = 'done'
elif verb == 'compact':
    if (roster_path.parent / 'compact_still_live').exists():
        print('stub turn still live', file=sys.stderr)
        sys.exit(3)
    before = entry.get('context_tokens', 0)
    entry['context_tokens'] = ('not_measured_since_compaction'
                              if (session / 'compact_reports_zero').exists() else 12000)
    roster_path.write_text(json.dumps(roster))
    if (session / 'compact_reports_zero').exists():
        print(f'compacted {name}: {before} tokens, size not measured until its next turn')
    else:
        print(f'compacted {name}: {before} -> 12000 tokens')
elif verb == 'can-follow':
    if entry.get('status') not in ('done', 'failed', 'paused'):
        print(f'codex_mesh: {name}: worker is busy or not followable; follow refused',
              file=sys.stderr)
        sys.exit(2)
    entry['previous_status'] = entry['status']
    if 'role' in entry:
        entry['previous_role'] = entry['role']
    entry['status'] = 'starting'
    entry['role'] = option('--role')
    entry['launcher_pid'] = int(option('--claim-pid'))
elif verb == 'release-follow':
    if (entry.get('status') == 'starting'
            and entry.get('launcher_pid') == int(option('--claim-pid'))):
        entry['status'] = entry.get('previous_status', 'failed')
        if 'previous_role' in entry:
            entry['role'] = entry['previous_role']
        else:
            entry.pop('role', None)
        entry.pop('launcher_pid', None)
        entry.pop('previous_status', None)
        entry.pop('previous_role', None)
elif verb == 'follow':
    if (entry.get('status') != 'starting'
            or entry.get('launcher_pid') != int(option('--claim-pid'))):
        print(f'codex_mesh: {name}: follow claim changed after dispatch', file=sys.stderr)
        sys.exit(1)
    entry['status'] = 'running'
    entry['role'] = option('--role')
    entry['task'] = option('--task')
    pathlib.Path(session / 'follow_message.txt').write_text(
        pathlib.Path(option('--message-file')).read_text()
    )
    finish_file = os.environ.get('STUB_REVIEW_FINISH_FILE', '')
    if finish_file:
        sys.path.insert(0, os.environ['TEST_WAIT_HELPER_DIR'])
        from wait_for_test_state import wait_for_file
        wait_for_file(pathlib.Path(finish_file))
    edit_path = os.environ.get('STUB_REVIEW_EDIT', '')
    if edit_path:
        pathlib.Path(edit_path).write_text('changed by reviewer\\n')
    if os.environ.get('STUB_REVIEW_COMMIT', ''):
        working_dir = os.environ['STUB_REVIEW_WORKING_DIR']
        subprocess.run(
            ['git', '-c', 'user.name=Review Test', '-c',
             'user.email=review@example.com', 'add', '.'],
            cwd=working_dir, check=True,
        )
        subprocess.run(
            ['git', '-c', 'user.name=Review Test', '-c',
             'user.email=review@example.com', 'commit', '-m', 'reviewer edit'],
            cwd=working_dir, check=True,
        )
    pathlib.Path(option('--summary-file')).write_text(os.environ.get('STUB_FINDINGS', 'No findings.\\n'))
    with pathlib.Path(option('--log-file')).open('a') as log:
        log.write('stub mesh log\\n')
    entry['status'] = 'done'
    if os.environ.get('STUB_REPLACEMENT_THREAD'):
        entry['thread_id'] = os.environ['STUB_REPLACEMENT_THREAD']
    if os.environ.get('STUB_TURN_FAILURE'):
        sys.exit(1)
else:
    sys.exit(2)
roster[name] = entry
roster_path.write_text(json.dumps(roster))
if verb == 'follow' and os.environ.get('STUB_REVIEW_DONE_FILE'):
    pathlib.Path(os.environ['STUB_REVIEW_DONE_FILE']).touch()
"""


STUB_POOL = r'''#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="verb", required=True)
for verb in ("allocate", "release"):
    command = sub.add_parser(verb)
    command.add_argument("--production", required=True)
    command.add_argument("--pid", type=int, required=True)
    if verb == "allocate":
        for flag in ("--type", "--unit", "--phase", "--pass"):
            command.add_argument(flag, required=True)
        command.add_argument("--lens", default="")
        command.add_argument("--fresh", action="store_true")
    else:
        command.add_argument("--reviewer", required=True)
        command.add_argument("--thread-id", default="")
args = parser.parse_args()
with Path(os.environ["STUB_POOL_CALLS"]).open("a") as calls:
    calls.write(json.dumps(vars(args)) + "\n")
if args.verb == "allocate":
    if os.environ.get("STUB_POOL_FAILURE"):
        sys.exit("review_pool: allocation failed")
    if os.environ.get("STUB_POOL_RETIRED"):
        print("review_pool: retired code0: reset failed", file=sys.stderr)
    print(json.dumps({"reviewer": os.environ.get("STUB_POOL_REVIEWER", "code1"), "thread_id": "thread-pool",
                      "mesh_dir": os.environ["STUB_POOL_MESH_DIR"],
                      "lease_ordinal": int(os.environ.get("STUB_LEASE_ORDINAL", "4")),
                      "prime_hash": "contract-hash"}))
'''

class OpenPass(TypedDict):
    kind: str
    status: str


# "pass" is a keyword, so the recorder's state key needs the functional form.
# The recorder keys the open passes by the team slot that opened each one, so
# the value is a map even when a single launcher is running.
ProgressState = TypedDict("ProgressState", {"pass": dict[str, OpenPass]})


AGENTS_REGISTRY = """[assignments]
direct=codex

[direct.codex]
implementation=gpt-called:xhigh
review=gpt-blind:max

[codex.agents]
gpt-called=low,medium,high,xhigh,max
gpt-blind=low,medium,high,xhigh,max

[direct.options]
codex_mesh=1
"""


class FixtureReadinessTests(unittest.TestCase):
    def assert_exit_wakes_wait(self, *, portable: bool = False, polling: bool = False) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "watched"
            directory.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            try:
                assert child.stdout is not None and child.stdin is not None
                self.assertEqual(child.stdout.readline(), "ready\n")
                checks = 0

                def exited() -> bool:
                    nonlocal checks
                    checks += 1
                    if checks == 1:
                        assert child.stdin is not None
                        child.stdin.close()
                        return False  # Exit races the wait, after registration.
                    return child.poll() is not None

                def polling_waiter(_a: Path, _b: Path) -> IntervalReleasePolling:
                    return IntervalReleasePolling()

                with ExitStack() as patches:
                    if portable:
                        _ = patches.enter_context(patch.object(os, "pidfd_open",
                            side_effect=AttributeError("pidfd_open unavailable"), create=True))
                    if polling:
                        _ = patches.enter_context(patch.object(wait_for_test_state,
                            "local_release_waiter", side_effect=polling_waiter))
                    # A shortened regression guard detects a lost wakeup; success
                    # is the exit predicate returning, without a timing assertion.
                    wait_until(exited, directory, timeout=5, processes=(child,))
                self.assertGreaterEqual(checks, 2)
                self.assertEqual(child.wait(timeout=300), 0)
                self.assertEqual(list(directory.iterdir()), [])
            finally:
                if child.poll() is None:
                    child.kill()
                _ = child.wait(timeout=300)
                if child.stdout is not None:
                    child.stdout.close()

    def test_child_exit_wakes_a_wait_without_a_directory_write(self) -> None:
        self.assert_exit_wakes_wait()

    def test_child_exit_wakes_a_wait_without_pidfd_support(self) -> None:
        self.assert_exit_wakes_wait(portable=True)

    def test_child_exit_wakes_a_wait_without_directory_notifications(self) -> None:
        self.assert_exit_wakes_wait(polling=True)


class ReviewLauncherPassTests(unittest.TestCase):
    """The early-launch contract: before the sentinel, no pass is recorded.

    The reviewer starts while the writer is still working, so the
    implementation pass is open the whole time it runs. A start-pass before the
    sentinel files a review pass for a verdict that cannot have read the final
    diff, and does it under the reviewer's own slot, where nothing later
    reconciles it against the diff it never saw.
    """

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    review_script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    prompt_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "project"
        self.working_dir.mkdir()
        _ = subprocess.run(
            ["git", "init", "-b", "feature/review"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        self.config_file = self.root / "direct.conf"
        _ = self.config_file.write_text(
            "\n".join([
                "UNIT_PROGRESS_INTERVAL_SECONDS=180",
                "UNIT_COMPACT_ABOVE_TOKENS=100000",
                "ROUTING_FROM_ROUND=4", "ASTRA_AFTER_FAILED_FIXES=2",
                "",
            ]),
            encoding="utf-8",
        )
        registry = self.root / ".claude" / "config" / "agents.conf"
        registry.parent.mkdir(parents=True)
        _ = registry.write_text(AGENTS_REGISTRY, encoding="utf-8")
        self.prompt_file = self.root / "review_prompt.md"
        _ = self.prompt_file.write_text("Review the diff.\n", encoding="utf-8")
        reviewed = self.working_dir / "reviewed.txt"
        _ = reviewed.write_text("original\n", encoding="utf-8")
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c",
             "user.email=review@example.com", "add", "reviewed.txt"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c",
             "user.email=review@example.com", "commit", "-m", "baseline"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        self.review_script = self.build_script_tree()

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_script_tree(self) -> Path:
        """Copy the real wrapper beside a stub agent, so nothing calls a model.

        Resolution, heartbeat writing, board and recorder helpers stay real.
        Mesh, agent execution and pool allocation are controlled so tests spend
        no model tokens. The real heartbeat watcher uses a short interval.
        """
        direct_dir = self.root / "scripts" / "direct"
        agents = self.root / "scripts" / "agents"
        lib = self.root / "scripts" / "lib"
        direct_dir.mkdir(parents=True)
        agents.mkdir(parents=True)
        lib.mkdir(parents=True)
        # The wrapper reaches python3 through ../lib/py, resolved from its own
        # directory.
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "py", lib / "py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "user_zone.py", lib / "user_zone.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "unit_runs.py", lib / "unit_runs.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "agents_registry.py", lib / "agents_registry.py")
        for name in (
            "review.sh", "implement.sh", "progress_history.py", "plan_history.py", "phase_reservation.py", "recorder_state.py",
            "findings.py", "review_tree.py", "board.sh", "worker_name.sh", "worker_state.py", "tag_inventory.py",
        ):
            _ = shutil.copy2(DIRECT_DIR / name, direct_dir / name)
        recorder = direct_dir / "progress_history.py"
        _ = recorder.write_text(recorder.read_text().replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n" + LEGACY_RECORDER_ARGUMENTS, 1,
        ))
        launcher = direct_dir / "review.sh"
        _ = launcher.write_text(
            launcher.read_text(encoding="utf-8").replace(
                "HEARTBEAT_INTERVAL_SECS=60", "HEARTBEAT_INTERVAL_SECS=1"
            ), encoding="utf-8",
        )
        for name in ("agents_config.sh", "agents_overlay.sh", "agents_overlay.awk", "heartbeat.sh", "heartbeat_watch.sh"):
            _ = shutil.copy2(AGENTS_DIR / name, agents / name)
        stub = agents / "agent_exec.sh"
        _ = stub.write_text(STUB_AGENT_EXEC, encoding="utf-8")
        stub.chmod(0o755)
        mesh = agents / "codex_mesh.py"
        _ = mesh.write_text(STUB_MESH, encoding="utf-8")
        mesh.chmod(0o755)
        _ = (direct_dir / "review_pool.py").write_text(STUB_POOL, encoding="utf-8")
        return direct_dir / "review.sh"

    def test_interpreter_cache_reuses_the_resolved_python_without_a_version_probe(self) -> None:
        launcher = self.root / "scripts" / "lib" / "py"
        binaries = self.root / "binaries"
        binaries.mkdir()
        interpreter = binaries / "python3"
        calls = self.root / "python-calls"
        _ = interpreter.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$2" >> "$CACHE_TEST_CALLS"\nexec "$CACHE_TEST_REAL_PYTHON" "$@"\n'
        )
        interpreter.chmod(0o755)
        environment = self.environment()
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "XDG_CACHE_HOME": str(self.root / "cold-cache"),
                            "CACHE_TEST_CALLS": str(calls), "CACHE_TEST_REAL_PYTHON": sys.executable})
        for _ in range(2):
            result = subprocess.run(["bash", str(launcher), "-c", "print('ready')"],
                                    env=environment, capture_output=True, text=True, timeout=300)
            self.assertEqual((result.returncode, result.stdout), (0, "ready\n"), result.stderr)
        commands = calls.read_text().splitlines()
        self.assertEqual(len(commands), 3)
        self.assertIn("sys.version_info", commands[0])
        self.assertEqual(commands[1:], ["print('ready')"] * 2)
        self.assertEqual((self.root / "cold-cache" / "claude" / "python3-3.10").read_text().strip(), str(interpreter))

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.root)
        environment["XDG_CACHE_HOME"] = str(self.root / "cache")
        cache = self.root / "cache" / "claude" / "python3-3.10"
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(sys.executable + "\n")
        _ = environment.pop("CLAUDE_PYTHON", None)
        environment["UNIT_EARLY_POLL_SECS"] = "0.01"
        environment["UNIT_WORKER_POLL_SECS"] = "0.01"
        environment["UNIT_WORKER_WAIT_ATTEMPTS"] = "30000"
        environment["UNIT_SYNC_WRITES"] = "0"
        environment["TEST_WAIT_HELPER_DIR"] = str(DIRECT_DIR.resolve())
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["TEST_LEGACY_LAUNCH"] = "1"
        environment["UNIT_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.root / ".claude" / "config" / "agents.conf")
        environment["TZ"] = "UTC"
        environment["USER_ZONE"] = "UTC"
        environment["REVIEW_POOL_STATE"] = str(self.root / "pool-state")
        _ = environment.pop("CODEX_THREAD_ID", None)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
        # Early mode starts a detached Stage 2 run; the stub records its arguments instead.
        mesh_runs = self.root / "mesh_runs_stub.py"
        if not mesh_runs.exists():
            _ = mesh_runs.write_text(
                "import json, os, sys\n"
                + "with open(os.environ['MESH_RUNS_CALLS'], 'a', encoding='utf-8') as calls:\n"
                + "    _ = calls.write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
        environment["MESH_RUNS_SCRIPT"] = str(mesh_runs)
        environment["MESH_RUNS_CALLS"] = str(self.root / "mesh_runs_calls.jsonl")
        return environment

    def recorder(self, *arguments: str) -> None:
        environment = self.environment()
        environment["UNIT_PASS_OWNER"] = "launcher"
        result = invoke_recorder(arguments, environment, Path.cwd())
        self.assertEqual(result.returncode, 0, result.stderr)

    def checked_review(
        self, session: Path, *options: str, lens: str = "", early: str = "",
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        _ = environment.pop("TEST_LEGACY_LAUNCH", None)
        environment["TEST_WORKER_STARTED"] = str(session / "review-worker-started")
        return subprocess.run(
            ["bash", str(self.review_script), *options, str(session), str(self.working_dir),
             str(self.prompt_file), "review", "blind review", "reviewing the diff", "1", lens, early],
            check=False, capture_output=True, text=True, env=environment, timeout=300,
        )

    def test_ux_launch_checks_identity_before_the_worker_and_records_valid_checks(self) -> None:
        stale = self.start_implementation("ux-stale-identity")
        self.finish_implementation(stale)
        _ = self.prompt_file.write_text("Phase 3 — Old title\nReview the layout.\n")
        refused = self.checked_review(stale, "--phase", "3", lens="ux")
        self.assertNotEqual(refused.returncode, 0, refused.stderr)
        self.assertFalse((stale / "review-worker-started").exists())
        rejected, = [row for row in self.events(stale.name) if row["event_type"] == "launch_refused"]
        self.assertEqual(rejected["prompt_file"], str(self.prompt_file))
        self.assertEqual(rejected["phase_id"], "3")
        self.assertTrue(rejected["reason"])
        self.assertEqual((stale / "phases/3/review_status_ux").read_text().strip(), "error")

        valid = self.start_implementation("ux-valid-identity")
        self.finish_implementation(valid)
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nReview the layout.\n")
        launched = self.checked_review(valid, "--phase", "3", lens="ux")
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertTrue((valid / "review-worker-started").exists())
        events = self.events(valid.name)
        checked, = [row for row in events if row["event_type"] == "launch_checked"]
        check = cast(dict[str, object], checked["launch_check"])
        self.assertEqual((check["phase_id"], check["phase_title"], check["prompt_phase_line"]),
                         ("3", "Retry handling", "matched"))
        self.assertFalse(any(row["event_type"] == "pass_started" and row["pass_kind"] == "review"
                             for row in events))

    def test_an_early_review_with_no_phase_refuses_before_starting_its_worker(self) -> None:
        session = self.start_implementation("early-missing-phase")
        self.finish_implementation(session)
        result = self.checked_review(session, early=str(session / "not-ready"))
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session / "review-worker-started").exists())
        self.assertEqual((session / "review_status").read_text().strip(), "error")
        refused, = [row for row in self.events(session.name) if row["event_type"] == "launch_refused"]
        self.assertEqual(refused["phase_id"], "")
        self.assertIn("phase", str(refused["reason"]).lower())
        self.assertFalse(any(row["event_type"] == "pass_started" and row["pass_kind"] == "review"
                             for row in self.events(session.name)))

    def test_review_passes_the_prompt_file_to_the_launch_check(self) -> None:
        session = self.start_implementation("checked-review")
        self.finish_implementation(session)
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nReview the diff.\n")
        result = self.checked_review(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        review, = [row for row in self.events(session.name)
                   if row["event_type"] == "pass_started" and row["pass_kind"] == "review"]
        check = cast(dict[str, object], review["launch_check"])
        self.assertEqual((check["phase_id"], check["phase_title"], check["prompt_phase_line"]),
                         ("3", "Retry handling", "matched"))
        self.assertTrue((session / "review-worker-started").exists())

    def test_review_stops_before_the_worker_on_a_recorder_refusal(self) -> None:
        for label, options, text in (
            ("stale-prompt", ("--phase", "3"), "Phase 3 — Wrong title\n"),
            ("missing-phase", (), "Review the diff.\n"),
        ):
            with self.subTest(label=label):
                session = self.start_implementation(f"checked-review-{label}")
                self.finish_implementation(session)
                _ = self.prompt_file.write_text(text)
                result = self.checked_review(session, *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((session / "review-worker-started").exists())
                records = self.events(session.name)
                self.assertFalse(any(row["event_type"] == "pass_started" and row["pass_kind"] == "review"
                                     for row in records))
                refused, = [row for row in records if row["event_type"] == "launch_refused"]
                self.assertEqual(refused["prompt_file"], str(self.prompt_file))
                self.assertEqual(refused["pass_kind"], "review")
                self.assertTrue(refused["reason"])
                self.assertIn("prompt" if label == "stale-prompt" else "phase", result.stderr.lower())

    def test_early_delivery_probes_default_to_five_seconds_without_test_overrides(self) -> None:
        session = self.start_implementation("default-early-poll")
        worker = self.open_worker(session)
        self.finish_implementation(session)
        ready, finish = session / "ready", session / "finish"
        binaries = session / "bin"
        binaries.mkdir()
        sleep = binaries / "sleep"
        _ = sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_POLL_ARGUMENTS"\n'
                             + ': > "$TEST_POLL_ENTERED"\n'
                             + ': > "$TEST_EARLY_READY"\n')
        sleep.chmod(0o755)
        entered = session / "poll-entered"
        arguments = session / "poll-arguments"
        environment = self.environment()
        for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                    "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
            _ = environment.pop(key, None)
        environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                            "TEST_POLL_ARGUMENTS": str(arguments), "TEST_POLL_ENTERED": str(entered),
                            "TEST_EARLY_READY": str(ready), "STUB_REVIEW_FINISH_FILE": str(finish)})
        process = subprocess.Popen(self.review_command(session, "adversary", str(ready), worker),
                                   env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            wait_until(entered.exists, session, processes=(process,))
            self.assertEqual(arguments.read_text().splitlines(), ["5"])
            self.assertIsNone(process.poll())
            wait_until(lambda: any(event.get("pass_kind") == "review" for event in self.pass_events(session.name)),
                       self.history_dir / "runs", processes=(process,))
        finally:
            output, errors = self.finish_early_launcher(process, ready, finish)
        self.assertEqual(process.returncode, 0, output + errors)

    def finish_early_launcher(self, process: subprocess.Popen[str], ready: Path,
                              finish: Path, timeout: float = 300) -> tuple[str, str]:
        ready.touch()
        finish.touch()
        return communicate_launcher(process, timeout)

    def test_early_launcher_timeout_reaps_its_group_before_fixture_cleanup(self) -> None:
        child_ready = self.root / "blocked-review-child"
        child_code = ("import os, signal, sys; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                      "p = Path(sys.argv[1]); staged = p.with_name(p.name + '.tmp'); "
                      "staged.write_text(str(os.getpid())); os.replace(staged, p); signal.pause()")
        program = f"import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(child_ready)!r}]); child.wait()"
        process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        ready, finish = self.root / "early-ready", self.root / "early-finish"
        try:
            descriptor = os.pidfd_open(wait_for_pid(child_ready, processes=(process,)))
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = self.finish_early_launcher(process, ready, finish, timeout=0)
                self.assertIsNotNone(process.poll())
                self.assertEqual(select.select([descriptor], [], [], 300)[0], [descriptor])
                self.assertTrue(ready.exists())
                self.assertTrue(finish.exists())
                self.assertTrue(process.stdout is not None and process.stdout.closed)
                self.assertTrue(process.stderr is not None and process.stderr.closed)
            finally:
                os.close(descriptor)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = process.communicate()

    def start_implementation(self, name: str) -> Path:
        session_dir = self.root / name
        session_dir.mkdir()
        plan = self.working_dir / "plan.md"
        _ = plan.write_text("## Delegation Context\n\n- **Project:** Test\n", encoding="utf-8")
        self.recorder(
            "start-run", "--session-dir", str(session_dir),
            "--working-dir", str(self.working_dir), "--plan-doc", "plan.md",
            "--main-family", "codex", "--main-model", "gpt-main",
            "--main-effort", "xhigh", "--main-session-id", f"main-{name}",
        )
        self.recorder(
            "start-phase", "--session-dir", str(session_dir),
            "--phase-id", "3", "--phase-title", "Retry handling",
        )
        self.recorder(
            "start-pass", "--session-dir", str(session_dir), "--phase-id", "3",
            "--pass-kind", "impl", "--activity", "writing the retry path",
            "--called-task", "direct.implementation", "--called-family", "codex",
            "--called-model", "gpt-called", "--called-effort", "xhigh",
        )
        return session_dir

    def review_command(
        self, session_dir: Path, lens: str, ready_file: str, to: str = "",
        new_slot: str = "",
    ) -> list[str]:
        command = [
            "bash", str(self.review_script), str(session_dir), str(self.working_dir),
            str(self.prompt_file), "review", "blind review", "reviewing the diff",
            "1", lens, ready_file,
        ]
        if to:
            command[2:2] = ["--to", to]
        elif new_slot:
            command[2:2] = ["--slot", new_slot]
        return command

    def run_review(self, session_dir: Path, ready_file: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.review_command(session_dir, "", str(ready_file)),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )

    def run_assigned_review(
        self,
        session_dir: Path,
        worker: str,
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
            self.review_command(session_dir, "", "", worker),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=300,
        )

    def finish_implementation(self, session_dir: Path) -> None:
        self.recorder(
            "finish-pass", "--session-dir", str(session_dir), "--status", "completed"
        )

    def open_worker(
        self, session_dir: Path, *, status: str = "done", context_tokens: int = 0
    ) -> str:
        name = "fixture-impl"
        _ = (session_dir / "mesh_roster.json").write_text(
            json.dumps({
                name: {
                    "thread_id": "thread-fixture",
                    "turn_id": "turn-1",
                    "status": status,
                    "role": "impl",
                    "context_tokens": context_tokens,
                }
            }),
            encoding="utf-8",
        )
        return name

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

    def pool_fixture(self, *, context_tokens: int = 0, reviewer_type: str = "code") -> dict[str, str]:
        mesh = self.root / "pool-state" / "production" / "mesh" / "7"
        mesh.mkdir(parents=True, exist_ok=True)
        _ = (mesh / "mesh_roster.json").write_text(json.dumps({f"{reviewer_type}1": {
            "thread_id": "thread-pool", "status": "done", "context_tokens": context_tokens,
        }}), encoding="utf-8")
        environment = self.environment()
        environment.update({
            "STUB_POOL_MESH_DIR": str(mesh),
            "STUB_POOL_REVIEWER": f"{reviewer_type}1",
            "STUB_POOL_CALLS": str(self.root / "pool-calls.jsonl"),
            "STUB_MESH_CALLS": str(self.root / "mesh-calls.jsonl"),
        })
        return environment

    def pool_command(self, session_dir: Path, *, lens: str = "contract", phase: str = "3",
                     early: str = "", reviewer_type: str = "code", pass_index: int = 1) -> list[str]:
        command = self.review_command(session_dir, lens, early)
        command[8] = str(pass_index)
        command[2:2] = ["--pool", reviewer_type, "--lease", f"production/unit/{phase}"]
        return command

    def turn_records(self, session_dir: Path) -> list[dict[str, object]]:
        directory = self.history_dir / "review_turns" / session_dir.name
        return [cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(directory.glob("*.json"))]

    def test_one_launcher_keeps_two_turn_records_with_the_same_start_second(self) -> None:
        session = self.start_implementation("two-turn-records")
        self.finish_implementation(session)
        # Exercise both writes in one launcher's exit path. The start time and
        # shell pid stay the same, while the finding counts distinguish them.
        source = self.review_script.read_text(encoding="utf-8")
        _ = self.review_script.write_text(source.replace(
            "  if ! write_turn_record; then\n",
            "  TURN_RAISED=3\n"
            + "  write_turn_record || exit 1\n"
            + "  TURN_RAISED=7\n"
            + "  if ! write_turn_record; then\n",
            1,
        ), encoding="utf-8")
        result = subprocess.run(self.pool_command(session), env=self.pool_fixture(),
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
        records = self.turn_records(session)
        self.assertEqual(len(records), 2)
        self.assertCountEqual([record["raised"] for record in records], [3, 7])
        self.assertEqual(records[0]["started_at"], records[1]["started_at"])
        for record in records:
            self.assertEqual(record["status"], "reviewed")
            self.assertEqual(record["thread_id"], "thread-pool")

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

    def test_pool_mesh_claim_compaction_release_use_allocated_dir_and_unit_records_stay_local(self) -> None:
        session = self.start_implementation("pool-routing")
        self.finish_implementation(session)
        environment = self.pool_fixture(context_tokens=100001)
        environment["STUB_POOL_RETIRED"] = "1"
        environment["STUB_FINDINGS"] = "1. First finding\n2) Second finding\n"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [cast("list[str]", json.loads(line)) for line in
                 (self.root / "mesh-calls.jsonl").read_text().splitlines()]
        self.assertEqual([call[0] for call in calls], ["compact", "can-follow", "follow", "release-follow"])
        for call in calls:
            self.assertEqual(call[call.index("--session-dir") + 1], environment["STUB_POOL_MESH_DIR"])
        follow = calls[2]
        self.assertEqual(follow[follow.index("--cwd") + 1], str(self.working_dir))
        self.assertIn("--read-only", follow)
        self.assertNotIn("--image", follow)
        for flag in ("--summary-file", "--reply-file", "--log-file"):
            self.assertEqual(Path(follow[follow.index(flag) + 1]).parent, session)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
        board = (session / "board.log").read_text()
        self.assertIn("[pool_code1] register:", board)
        self.assertIn("compacted code1", board)
        self.assertFalse((Path(environment["STUB_POOL_MESH_DIR"]) / "board.log").exists())
        self.assertIn("review_pool: retired code0: reset failed", (session / "review_agent_1_contract-pool.log").read_text())
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate", "release"])
        self.assertFalse(self.call_records("pool-calls.jsonl")[0]["fresh"])
        events = [event for event in self.pass_events(session.name) if event.get("pass_kind") == "review"]
        self.assertEqual([event.get("team_slot") for event in events], ["pool_code1", "pool_code1"])
        record, = self.turn_records(session)
        self.assertEqual({key: record[key] for key in ("arm", "pool", "reviewer", "thread_id", "unit", "phase", "kind", "lens", "pass", "lease_ordinal", "prime_hash", "status", "raised")},
                         {"arm": "pool", "pool": "production", "reviewer": "code1", "thread_id": "thread-pool", "unit": "unit", "phase": "3", "kind": "broad", "lens": "contract", "pass": 1, "lease_ordinal": 4, "prime_hash": "contract-hash", "status": "reviewed", "raised": 2})
        state = cast("dict[str, object]", json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(record["phase_instance_id"], cast("dict[str, object]", state["phase"])["instance_id"])
        self.assertEqual(record["history_file"], state["history_file"])
        self.assertLessEqual(cast(float, record["started_at"]), cast(float, record["finished_at"]))

    def test_a_first_pool_lease_delivers_the_original_prompt(self) -> None:
        session = self.start_implementation("pool-first-lease")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_LEASE_ORDINAL"] = "1"
        command = self.pool_command(session)
        original = Path(command[8]).read_text()
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((Path(environment["STUB_POOL_MESH_DIR"]) / "follow_message.txt").read_text(), original)
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate", "release"])

    def test_pool_compaction_with_live_turn_refuses_and_releases_without_follow(self) -> None:
        session = self.start_implementation("pool-live")
        self.finish_implementation(session)
        environment = self.pool_fixture(context_tokens=100001)
        (Path(environment["STUB_POOL_MESH_DIR"]) / "compact_still_live").touch()
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((Path(environment["STUB_POOL_MESH_DIR"]) / "mesh_events.txt").read_text().splitlines(), ["compact"])
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate", "release"])
        self.assertEqual((session / "review_status_contract").read_text().strip(), "error")
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_failed_pool_attempt_and_own_retry_leave_one_record_each_and_ignore_review_lease(self) -> None:
        session = self.start_implementation("pool-retry")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_POOL_FAILURE"] = "1"
        failed = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual((session / "review_status_contract").read_text().strip(), "error")
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate"])
        worker = self.open_worker(session)
        retry = subprocess.run(self.review_command(session, "contract", "", worker), env=environment,
                               capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        records = {str(record["arm"]): record for record in self.turn_records(session)}
        self.assertEqual(set(records), {"pool", "own"})
        self.assertEqual(records["pool"]["status"], "error")
        self.assertEqual(records["own"]["status"], "reviewed")
        self.assertEqual(records["own"]["thread_id"], "thread-fixture")
        self.assertEqual((records["own"]["unit"], records["own"]["phase"]), ("", ""))

    def test_own_turn_records_survive_two_phases_in_the_same_session(self) -> None:
        session = self.start_implementation("two-phases")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        first = self.run_assigned_review(session, worker)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_record, = self.turn_records(session)
        self.recorder("finish-phase", "--session-dir", str(session), "--status", "completed")
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", 'Next "phase"')
        second = self.run_assigned_review(session, worker)
        self.assertEqual(second.returncode, 0, second.stderr)
        records = self.turn_records(session)
        self.assertEqual(len(records), 2)
        self.assertIn(first_record, records)
        self.assertEqual(len({str(record["phase_instance_id"]) for record in records}), 2)
        self.assertTrue(all(record["unit"] == record["phase"] == "" for record in records))

    def test_own_ux_lens_records_ux_kind_without_pass_calls(self) -> None:
        session = self.start_implementation("own-ux")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        environment = self.environment()
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.review_command(session, "ux", "", worker), env=environment,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["kind"], "ux")
        self.assertEqual(self.call_records("pass-calls.jsonl"), [])

    def test_code_pool_review_still_starts_and_finishes_a_pass(self) -> None:
        session = self.start_implementation("code-pass")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["verb"] for call in self.call_records("pass-calls.jsonl")],
                         ["start-pass", "finish-pass"])

    def test_a_review_naming_an_older_open_phase_starts_its_pass_in_that_phase(self) -> None:
        session = self.start_implementation("named-phase-pass")
        self.finish_implementation(session)
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Next phase")
        environment = self.pool_fixture()
        self.trace_pass_recorder(environment)
        command = self.pool_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        start, = [call for call in self.call_records("pass-calls.jsonl") if call["verb"] == "start-pass"]
        self.assertEqual(cast("list[str]", start["argv"])[-2:], ["--phase-id", "3"])

    def test_a_pool_review_naming_an_older_open_phase_records_its_turn_in_that_phase(self) -> None:
        session = self.start_implementation("named-phase-turn")
        self.finish_implementation(session)
        older = cast("dict[str, object]", json.loads(
            (session / "progress_history_state.json").read_text(encoding="utf-8"))["phase"])
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Next phase")
        environment = self.pool_fixture()
        environment["STUB_FINDINGS"] = "1. First finding\n2. Second finding\n"
        command = self.pool_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        record, = self.turn_records(session)
        current = cast("dict[str, object]", json.loads(
            (session / "progress_history_state.json").read_text(encoding="utf-8"))["phase"])
        self.assertNotEqual(older["instance_id"], current["instance_id"])
        self.assertEqual(record["phase_instance_id"], older["instance_id"])
        self.assertEqual(record["raised"], 2)

    def test_a_review_naming_its_phase_writes_its_files_in_that_phase_directory(self) -> None:
        # Two phases reviewing at once each pass their own id, so neither reads the other's findings.
        session = self.start_implementation("phase-directory-files")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        command = self.pool_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        phase_dir = session / "phases" / "3"
        self.assertTrue(list(phase_dir.glob("review_findings_*.txt")))
        self.assertTrue(list(phase_dir.glob("review_status*")))
        self.assertEqual(sorted(path.name for path in session.glob("review_*")), [])

    def test_a_pool_review_names_the_director_in_the_unit_run_folder(self) -> None:
        session = self.start_implementation("director")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True, text=True,
                                check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session / "director").read_text(encoding="utf-8"), "sid-director\n")

    def test_a_phase_id_with_a_space_names_a_directory_without_it(self) -> None:
        session = self.start_implementation("phase-directory-name")
        self.finish_implementation(session)
        command = self.pool_command(session)
        command[2:2] = ["--phase", "ad hoc"]
        result = subprocess.run(command, env=self.pool_fixture(), capture_output=True, text=True,
                                check=False, timeout=300)
        self.assertNotEqual(result.returncode, 2, result.stderr)
        self.assertTrue(list((session / "phases" / "adhoc").glob("review_status*")))

    def test_ui_pool_reviewed_turn_records_ux_kind_without_pass_calls_at_either_start_site(self) -> None:
        session = self.start_implementation("ui-no-pass")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        self.trace_pass_recorder(environment)
        ready = session / "ready"
        ready.touch()
        for early in ("", str(ready)):
            with self.subTest(early=early):
                result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux", early=early),
                                        env=environment, capture_output=True, text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.call_records("pass-calls.jsonl"), [])
                self.assertEqual((session / "review_status_ux").read_text().strip(), "reviewed")
                self.assertTrue(all(record["kind"] == "ux" and record["status"] == "reviewed"
                                    for record in self.turn_records(session)))
                self.assertTrue(self.turn_records(session))

    def test_ui_pool_failed_turn_records_error_without_pass_calls(self) -> None:
        session = self.start_implementation("ui-error-no-pass")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_TURN_FAILURE"] = "1"
        self.trace_pass_recorder(environment)
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=environment, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.call_records("pass-calls.jsonl"), [])
        self.assertEqual((session / "review_status_ux").read_text().strip(), "error")
        record, = self.turn_records(session)
        self.assertEqual((record["kind"], record["status"]), ("ux", "error"))

    def test_ui_pool_refuses_non_ux_lens_before_allocation_or_turn(self) -> None:
        session = self.start_implementation("ui-wrong-lens")
        self.finish_implementation(session)
        environment = self.pool_fixture(reviewer_type="ui")
        for lens in ("", "contract", "adversary", "craft"):
            with self.subTest(lens=lens):
                result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens=lens),
                                        env=environment, capture_output=True, text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr, "ERROR: --pool ui requires lens ux.\n")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())

    def test_code_pool_refuses_ux_lens_before_allocation_or_turn(self) -> None:
        session = self.start_implementation("code-wrong-lens")
        self.finish_implementation(session)
        result = subprocess.run(self.pool_command(session, lens="ux"), env=self.pool_fixture(),
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr, "ERROR: --pool code does not accept lens ux.\n")
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())

    def test_ui_pool_allocates_without_a_reset_and_sends_each_shot_on_the_requested_check(self) -> None:
        session = self.start_implementation("ui-shots")
        self.finish_implementation(session)
        shots = [str(self.root / "shot one.png"), str(self.root / "café.png")]
        _ = (session / "ux_shots_1.txt").write_text("unused.png\n", encoding="utf-8")
        _ = (session / "ux_shots_2.txt").write_text(shots[0] + "\r\n\n" + shots[1], encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux", pass_index=2),
                                env=environment, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        allocation = self.call_records("pool-calls.jsonl")[0]
        self.assertFalse(allocation["fresh"])
        self.assertEqual(allocation["type"], "ui")
        self.assertEqual(allocation["lens"], "ux")
        self.assertEqual(allocation["pass"], "2")
        calls = [cast("list[str]", json.loads(line)) for line in
                 (self.root / "mesh-calls.jsonl").read_text().splitlines()]
        follow, = [call for call in calls if call[0] == "follow"]
        self.assertEqual([follow[index + 1] for index, value in enumerate(follow) if value == "--image"], shots)
        self.assertEqual((session / "review_slot_ux").read_text().strip(), "pool_ui1")
        record, = self.turn_records(session)
        self.assertEqual((record["reviewer"], record["kind"], record["pass"]), ("ui1", "ux", 2))

    def test_ui_pool_failed_allocation_records_error_without_a_turn(self) -> None:
        session = self.start_implementation("ui-allocation-error")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_POOL_FAILURE"] = "1"
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=environment, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")], ["allocate"])
        self.assertFalse(self.call_records("pool-calls.jsonl")[0]["fresh"])
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())
        self.assertEqual((session / "review_status_ux").read_text().strip(), "error")
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_ui_pool_turn_sends_the_prompt_as_written(self) -> None:
        session = self.start_implementation("ui-prompt")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_LEASE_ORDINAL"] = "2"
        command = self.pool_command(session, reviewer_type="ui", lens="ux")
        original = Path(command[8]).read_text()
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((Path(environment["STUB_POOL_MESH_DIR"]) / "follow_message.txt").read_text(), original)
        self.assertEqual([call["verb"] for call in self.call_records("pool-calls.jsonl")],
                         ["allocate", "release"])
        self.assertEqual(list(session.glob("review_message_*")), [])

    def test_ui_pool_missing_shots_file_records_error_without_allocation(self) -> None:
        session = self.start_implementation("ui-missing-shots")
        self.finish_implementation(session)
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=self.pool_fixture(reviewer_type="ui"), capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("UI shots file is unreadable", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_ui_pool_empty_shots_file_records_error_without_allocation(self) -> None:
        session = self.start_implementation("ui-empty-shots")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("\n", encoding="utf-8")
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"),
                                env=self.pool_fixture(reviewer_type="ui"), capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("UI shots file has no paths", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())
        self.assertFalse((self.root / "mesh-calls.jsonl").exists())
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_ux_raised_counts_defect_rows_without_headers_candidates_or_numbered_text(self) -> None:
        session = self.start_implementation("ux-defect-count")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_FINDINGS"] = (
            "Verdict: defects\n\n"
            "| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
            "| a.png | cables-drape-smoothly | sag crosses the face | lift the cable |\n"
            "| b.png | no rule | label cut at the right edge | widen the field\n"
            "| c.png | widgets-follow-macos | reads `on \\| off` as one word | split the label |\n\n"
            "Canonical candidates\n\n"
            "| Shot | Rule | Slot | Crop (full-size px) | Why it is clear |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| /tmp/a.png | cables-drape-smoothly | bad | 40x40+1+1 | one cable |\n\n"
            "1. Other text\n"
        )
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"), env=environment,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 3)

    def test_ux_pass_with_only_table_headers_records_zero_raised(self) -> None:
        session = self.start_implementation("ux-pass-count")
        self.finish_implementation(session)
        _ = (session / "ux_shots_1.txt").write_text("shot.png\n", encoding="utf-8")
        environment = self.pool_fixture(reviewer_type="ui")
        environment["STUB_FINDINGS"] = "Verdict: pass\n\n| Shot | Rule | Defect | Fix |\n| --- | --- | --- | --- |\n"
        result = subprocess.run(self.pool_command(session, reviewer_type="ui", lens="ux"), env=environment,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 0)

    def test_pool_failed_turn_releases_claim_and_pool_and_records_findings(self) -> None:
        session = self.start_implementation("pool-failed-turn")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_TURN_FAILURE"] = "1"
        environment["STUB_FINDINGS"] = "1. Partial finding\n"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.turn_records(session)[0]["raised"], 1)
        self.assertEqual(self.turn_records(session)[0]["status"], "error")
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")
        self.assertIn("release-follow", (self.root / "mesh-calls.jsonl").read_text())

    def test_pool_tree_change_is_refused_and_releases_allocation(self) -> None:
        session = self.start_implementation("pool-edit")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        environment["STUB_REVIEW_EDIT"] = str(self.working_dir / "reviewed.txt")
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changed the working tree", result.stderr)
        self.assertEqual(self.turn_records(session)[0]["status"], "error")
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")

    def side_by_side_review(self, name: str, edited: str, *, commit_change: bool = False) -> tuple[
            subprocess.CompletedProcess[str], Path]:
        """Review phase 3 while phase 4, open beside it, holds `other.txt` and its writer edits `edited`."""
        session = self.start_implementation(name)
        self.finish_implementation(session)
        # The stub's commit adds every file, so the plan start_implementation writes is committed first.
        _ = subprocess.run(["git", "add", "plan.md"], cwd=self.working_dir, check=True, capture_output=True)
        _ = subprocess.run(
            ["git", "-c", "user.name=Review Test", "-c", "user.email=review@example.com",
             "commit", "-q", "-m", "plan"],
            cwd=self.working_dir, check=True, capture_output=True, text=True,
        )
        for phase, files in (("3", "reviewed.txt\n"), ("4", "other.txt\n")):
            directory = session / "phases" / phase
            directory.mkdir(parents=True, exist_ok=True)
            _ = (directory / "delegated_phase_reservation_state.json").write_text(
                json.dumps({"state": "active", "phase": phase}), encoding="utf-8")
            _ = (directory / "files").write_text(files, encoding="utf-8")
        # A phase is open while the recorder lists it, so phase 4 starts there beside phase 3.
        self.recorder("start-phase", "--session-dir", str(session), "--phase-id", "4", "--phase-title", "Parallel writer")
        environment = self.pool_fixture()
        environment["STUB_REVIEW_EDIT"] = str(self.working_dir / edited)
        environment["STUB_REVIEW_WORKING_DIR"] = str(self.working_dir)
        if commit_change:
            environment["STUB_REVIEW_COMMIT"] = "1"
        command = self.pool_command(session)
        command[2:2] = ["--phase", "3"]
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=300)
        return result, session / "phases" / "3"

    def test_a_parallel_phase_writing_its_own_files_during_a_review_leaves_the_review_passed(self) -> None:
        result, phase_dir = self.side_by_side_review("parallel-writer", "other.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("changed the working tree", result.stderr)
        self.assertEqual((phase_dir / "review_status_contract").read_text(encoding="utf-8"), "reviewed\n")
        log = "".join(path.read_text(encoding="utf-8") for path in phase_dir.glob("review_agent_1*.log"))
        self.assertIn("open phase 4's files changed during the review: other.txt", log)
        self.assertEqual(list(phase_dir.glob("review_tree_*")), [])

    def test_a_parallel_phase_commit_during_a_review_leaves_the_review_passed(self) -> None:
        result, phase_dir = self.side_by_side_review("parallel-commit", "other.txt", commit_change=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((phase_dir / "review_status_contract").read_text(encoding="utf-8"), "reviewed\n")

    def test_beside_a_parallel_phase_an_edit_to_the_reviewed_phases_file_still_fails(self) -> None:
        result, phase_dir = self.side_by_side_review("parallel-own-edit", "reviewed.txt")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changed the working tree: reviewed.txt", result.stderr)
        self.assertEqual((phase_dir / "review_status_contract").read_text(encoding="utf-8"), "error\n")

    def test_beside_a_parallel_phase_an_edit_no_phase_holds_still_fails(self) -> None:
        result, _ = self.side_by_side_review("parallel-unheld-edit", "loose.txt")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("changed the working tree: loose.txt", result.stderr)

    def test_pool_excludes_to_and_slot_before_any_allocation(self) -> None:
        session = self.start_implementation("pool-exclusive")
        environment = self.pool_fixture()
        for option in ("--to", "--slot"):
            command = self.pool_command(session)
            command[2:2] = [option, "fixture-impl"]
            result = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, check=False, timeout=300)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("excludes --to and --slot", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_pool_requires_codex_mesh_before_allocation(self) -> None:
        session = self.start_implementation("pool-mesh-off")
        environment = self.pool_fixture()
        environment["UNIT_CODEX_MESH"] = "0"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("requires the Codex mesh", result.stderr)
        self.assertFalse((self.root / "pool-calls.jsonl").exists())

    def test_pool_claim_refusal_releases_allocation_and_records_error(self) -> None:
        session = self.start_implementation("pool-busy")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        mesh = Path(environment["STUB_POOL_MESH_DIR"])
        _ = (mesh / "mesh_roster.json").write_text(json.dumps({"code1": {
            "thread_id": "thread-pool", "status": "running",
        }}), encoding="utf-8")
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("worker is busy or not followable", result.stderr)
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")
        self.assertEqual(self.turn_records(session)[0]["status"], "error")

    def test_pool_early_launch_defers_pass_until_delivery_and_keeps_turn_record(self) -> None:
        session = self.start_implementation("pool-early")
        environment = self.pool_fixture()
        ready = session / "ready"
        finish = self.root / "finish"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        process = subprocess.Popen(self.pool_command(session, early=str(ready)), env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            mesh = Path(environment["STUB_POOL_MESH_DIR"])
            wait_until(lambda: (mesh / "follow_message.txt").exists() or process.poll() is not None, mesh, processes=(process,))
            self.assertIsNone(process.poll(), "review must remain active before delivery")
            self.assertTrue((mesh / "follow_message.txt").exists())
            self.assertFalse(any(event.get("pass_kind") == "review" for event in self.pass_events(session.name)))
            self.finish_implementation(session)
            ready.touch()
            finish.touch()
            _, errors = process.communicate(timeout=300)
            self.assertEqual(process.returncode, 0, errors)
        finally:
            finish.touch()
            if process.poll() is None:
                process.terminate()
            _ = process.communicate(timeout=300)
        self.assertEqual(self.turn_records(session)[0]["status"], "reviewed")
        self.assertEqual(len([event for event in self.pass_events(session.name) if event.get("pass_kind") == "review"]), 2)

    def test_pool_signal_leaves_pass_open_for_director_cancel_and_releases_reviewer(self) -> None:
        session = self.start_implementation("pool-signal")
        self.finish_implementation(session)
        environment = self.pool_fixture()
        finish = self.root / "finish"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        process = subprocess.Popen(self.pool_command(session), env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            mesh = Path(environment["STUB_POOL_MESH_DIR"])
            wait_until(lambda: (mesh / "follow_message.txt").exists() or process.poll() is not None, mesh, processes=(process,))
            self.assertIsNone(process.poll(), "review must remain active until signal")
            self.assertTrue((mesh / "follow_message.txt").exists())
            process.send_signal(signal.SIGTERM)
            _, errors = process.communicate(timeout=300)
            self.assertEqual(process.returncode, 143, errors)
        finally:
            finish.touch()
            if process.poll() is None:
                process.terminate()
            _ = process.communicate(timeout=300)
        self.assertEqual(self.call_records("pool-calls.jsonl")[-1]["verb"], "release")
        state = cast(ProgressState, json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"]["pool_code1"]["status"], "active")
        slot = (session / "review_slot_contract").read_text().strip()
        self.assertEqual(slot, "pool_code1")
        environment["UNIT_TEAM_ROLE"] = slot
        _ = environment.pop("UNIT_PASS_OWNER", None)
        canceled = subprocess.run(
            ["python3", str(DIRECT_DIR / "progress_history.py"), "finish-pass",
             "--session-dir", str(session), "--status", "canceled", "--orphaned-launcher"],
            env=environment, capture_output=True, text=True, check=False, timeout=300,
        )
        self.assertEqual(canceled.returncode, 0, canceled.stderr)
        state = cast(ProgressState, json.loads((session / "progress_history_state.json").read_text()))
        self.assertEqual(state["pass"][slot]["status"], "canceled")
        self.assertEqual(self.turn_records(session)[0]["status"], "canceled")
        self.assertIn("release-follow", (self.root / "mesh-calls.jsonl").read_text())

    def test_pool_writes_allocated_slot_with_the_status_suffix(self) -> None:
        for lens in ("contract", ""):
            with self.subTest(lens=lens):
                session = self.start_implementation(f"pool-slot-{lens or 'closure'}")
                self.finish_implementation(session)
                result = subprocess.run(self.pool_command(session, lens=lens), env=self.pool_fixture(),
                                        capture_output=True, text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr)
                suffix = f"_{lens}" if lens else ""
                self.assertEqual((session / f"review_slot{suffix}").read_text(), "pool_code1\n")

    def test_failed_pool_allocation_removes_stale_slot(self) -> None:
        session = self.start_implementation("pool-stale-slot")
        self.finish_implementation(session)
        slot = session / "review_slot_contract"
        _ = slot.write_text("pool_code99\n", encoding="utf-8")
        environment = self.pool_fixture()
        environment["STUB_POOL_FAILURE"] = "1"
        result = subprocess.run(self.pool_command(session), env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(slot.exists())

    def test_early_verdict_without_delivery_records_void_and_findings_without_a_pass(self) -> None:
        for arm in ("pool", "own"):
            with self.subTest(arm=arm):
                session = self.start_implementation(f"void-{arm}")
                ready = session / "ready"
                if arm == "pool":
                    environment = self.pool_fixture()
                    command = self.pool_command(session, early=str(ready))
                else:
                    environment = self.environment()
                    command = self.review_command(session, "contract", str(ready), self.open_worker(session))
                environment["STUB_FINDINGS"] = "1. A void finding\n"
                result = subprocess.run(command, env=environment, capture_output=True,
                                        text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((session / "review_status_contract").read_text().strip(), "reviewed")
                record, = self.turn_records(session)
                self.assertEqual(record["status"], "void")
                self.assertEqual(record["raised"], 1)
                self.assertFalse(any(event.get("pass_kind") == "review" for event in self.pass_events(session.name)))

    def test_own_term_records_canceled_instead_of_successful_exit_trap(self) -> None:
        session = self.start_implementation("own-signal")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        environment = self.environment()
        finish = self.root / "finish"
        done = self.root / "done"
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish)
        environment["STUB_REVIEW_DONE_FILE"] = str(done)
        # The own arm retains its existing child behavior. Files keep the
        # child's inherited output pipes from hiding the launcher's exit.
        with (self.root / "signal-output.txt").open("w+") as output:
            process = subprocess.Popen(self.review_command(session, "", "", worker), env=environment,
                                       stdout=output, stderr=output, text=True)
            try:
                wait_until(lambda: (session / "follow_message.txt").exists() or process.poll() is not None, session, processes=(process,))
                self.assertIsNone(process.poll())
                self.assertTrue((session / "follow_message.txt").exists())
                process.send_signal(signal.SIGTERM)
                code = process.wait(timeout=300)
                record, = self.turn_records(session)
                self.assertEqual(record["status"], "canceled")
                self.assertEqual(code, 143)
            finally:
                finish.touch()
                if process.poll() is None:
                    process.terminate()
                _ = process.wait(timeout=300)
                wait_for_file(done)
                self.assertTrue(done.exists(), "the owned stub child must finish before fixture cleanup")

    def test_replaced_thread_is_recorded_from_the_turn_roster_and_released_to_pool(self) -> None:
        for arm in ("pool", "own"):
            with self.subTest(arm=arm):
                session = self.start_implementation(f"replacement-{arm}")
                self.finish_implementation(session)
                if arm == "pool":
                    environment = self.pool_fixture()
                    command = self.pool_command(session)
                else:
                    environment = self.environment()
                    command = self.review_command(session, "", "", self.open_worker(session))
                environment["STUB_REPLACEMENT_THREAD"] = "thread-replacement"
                result = subprocess.run(command, env=environment, capture_output=True,
                                        text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.turn_records(session)[0]["thread_id"], "thread-replacement")
                if arm == "pool":
                    release = self.call_records("pool-calls.jsonl")[-1]
                    self.assertEqual(release["verb"], "release")
                    self.assertEqual(release["thread_id"], "thread-replacement")

    def test_unknown_lens_lists_ux_among_accepted_lenses(self) -> None:
        session = self.start_implementation("unknown-lens")
        result = subprocess.run(self.review_command(session, "unknown", ""), env=self.environment(),
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("adversary, contract, craft, ux", result.stderr)

    def test_own_failed_closure_turn_records_its_findings_count(self) -> None:
        session = self.start_implementation("own-failed")
        self.finish_implementation(session)
        worker = self.open_worker(session)
        environment = self.environment()
        environment["STUB_TURN_FAILURE"] = "1"
        environment["STUB_FINDINGS"] = "1. Partial finding\n"
        command = self.review_command(session, "", "", worker)
        command[-3] = "2"
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 1, result.stderr)
        record, = self.turn_records(session)
        self.assertEqual(record["kind"], "closure")
        self.assertEqual(record["pass"], 2)
        self.assertEqual(record["raised"], 1)
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["phase"], "")

    def test_a_reviewer_that_finishes_before_delivery_records_no_pass(self) -> None:
        session_dir = self.start_implementation("early")
        ready = session_dir / "final_diff_1.ready"
        result = self.run_review(session_dir, ready)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "review_status").read_text().strip(), "reviewed")

        recorded = self.pass_events("early")
        self.assertEqual(
            [event["event_type"] for event in recorded],
            ["pass_started"],
            "the implementation pass must be the only pass on the record",
        )
        self.assertEqual(recorded[0].get("pass_kind"), "impl")

        state = cast(
            ProgressState,
            json.loads((session_dir / "progress_history_state.json").read_text()),
        )
        open_passes = state["pass"]
        self.assertEqual(len(open_passes), 1)
        open_pass = next(iter(open_passes.values()))
        self.assertEqual(open_pass["kind"], "impl")
        self.assertEqual(open_pass["status"], "active")

    def test_a_reviewer_records_its_pass_once_the_sentinel_appears(self) -> None:
        session_dir = self.start_implementation("delivered")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        ready = session_dir / "final_diff_1.ready"
        _ = (session_dir / "final_diff_1.diff").write_text("diff\n", encoding="utf-8")
        _ = ready.write_text("", encoding="utf-8")

        result = self.run_review(session_dir, ready)
        self.assertEqual(result.returncode, 0, result.stderr)

        kinds = [
            (event["event_type"], event.get("pass_kind"))
            for event in self.pass_events("delivered")
        ]
        self.assertEqual(
            kinds,
            [
                ("pass_started", "impl"),
                ("pass_finished", "impl"),
                ("pass_started", "review"),
                ("pass_finished", "review"),
            ],
        )

    def test_paused_reviewer_receives_followup_on_its_recorded_thread(self) -> None:
        session_dir = self.start_implementation("paused-review")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, status="paused")
        result = self.run_assigned_review(session_dir, worker)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text().splitlines(), ["can-follow", "follow"])
        roster = cast(dict[str, dict[str, object]], json.loads((session_dir / "mesh_roster.json").read_text()))
        self.assertEqual(roster[worker]["thread_id"], "thread-fixture")
        self.assertEqual((session_dir / "follow_message.txt").read_text(), "Review the diff.\n")
        self.assertEqual((session_dir / "review_status").read_text(), "reviewed\n")

    def test_a_named_open_worker_receives_the_review_and_writes_findings(self) -> None:
        session_dir = self.start_implementation("assigned")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "follow_message.txt").read_text(encoding="utf-8"),
            "Review the diff.\n",
        )
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        roster = cast(
            "dict[str, dict[str, object]]",
            json.loads((session_dir / "mesh_roster.json").read_text(encoding="utf-8")),
        )
        self.assertEqual(roster[worker]["role"], "review")
        self.assertEqual(roster[worker]["task"], "direct.review")
        reviews = [
            event for event in self.pass_events("assigned")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "impl"), ("pass_finished", "impl")],
        )
        self.assertEqual(reviews[0]["worker_name"], worker)

    def test_a_review_opened_worker_has_durable_identity_for_an_implementation_claim(self) -> None:
        session_dir = self.start_implementation("new-worker")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review2"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["start"],
        )
        roster = cast(
            "dict[str, dict[str, object]]",
            json.loads((session_dir / "mesh_roster.json").read_text(encoding="utf-8")),
        )
        worker = "project-review2"
        self.assertEqual(roster[worker]["status"], "done")
        self.assertEqual(roster[worker]["role"], "review")
        self.assertEqual(roster[worker]["task"], "direct.review")
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        # The seat's log is its own, so a second seat on pass 1 cannot truncate it.
        self.assertTrue((session_dir / "review_agent_1-review2.log").is_file())
        self.assertEqual(os.readlink(session_dir / "review_agent.log"), "review_agent_1-review2.log")
        reviews = [
            event for event in self.pass_events("new-worker")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(
            [(event["event_type"], event.get("team_slot")) for event in reviews],
            [("pass_started", "review2"), ("pass_finished", "review2")],
        )
        identity = cast(
            "dict[str, object]",
            json.loads((session_dir / "impl_worker_review2.json").read_text()),
        )
        self.assertEqual(identity, {
            "name": worker,
            "slot": "review2",
            "family": "codex",
            "model": "gpt-called",
            "worker_id": f"thread-{worker}",
        })

        implementation = subprocess.run(
            [
                "bash", str(self.review_script.with_name("implement.sh")),
                "--to", worker, str(session_dir), str(self.working_dir),
                str(self.prompt_file), "implementation", "write the repair",
                "impl", "writing the repair", "0", "review2",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )
        self.assertEqual(implementation.returncode, 0, implementation.stderr)
        self.assertEqual(
            (session_dir / "impl_status_review2").read_text(encoding="utf-8"),
            "implemented\n",
        )

    def test_a_test_worker_keeps_one_identity_when_assigned_a_review(self) -> None:
        run = self.start_implementation("test-becomes-review")
        self.finish_implementation(run)
        worker = "project-test"
        identity = {"name": worker, "slot": "test", "role": "test", "family": "codex",
                    "model": "gpt-called", "worker_id": "thread-test"}
        path = run / "impl_worker_test.json"
        _ = path.write_text(json.dumps(identity))
        _ = (run / "mesh_roster.json").write_text(json.dumps({worker: {
            "thread_id": "thread-test", "status": "done", "role": "test", "context_tokens": 0}}))
        _ = (run / "workers").write_text(f"thread-test\t{worker}\n")

        reviewed = self.run_assigned_review(run, worker)

        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        records = [cast(dict[str, object], json.loads(item.read_text()))
                   for item in run.glob("impl_worker_*.json")]
        identities = [record for record in records if record.get("name") == worker]
        self.assertEqual(len(identities), 1, records)
        self.assertEqual(identities[0]["worker_id"], "thread-test")
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text()))["name"], worker)
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        self.assertEqual(roster[worker]["role"], "review")

    def test_hyphenated_new_slot_is_refused_to_keep_followup_identity_stable(self) -> None:
        session_dir = self.start_implementation("hyphenated-slot")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", "", new_slot="review-extra"),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("alphanumeric/_ characters", result.stderr)
        self.assertFalse((session_dir / "mesh_roster.json").exists())

    def test_an_early_assigned_review_baselines_after_the_writers_final_edit(self) -> None:
        session_dir = self.start_implementation("early-writer-edit")
        worker = self.open_worker(session_dir)
        ready = session_dir / "final_diff_1.ready"
        finish_review = session_dir / "finish_review"
        environment = self.environment()
        environment["STUB_REVIEW_FINISH_FILE"] = str(finish_review)
        process = subprocess.Popen(
            self.review_command(session_dir, "adversary", str(ready), worker),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

        _ = (self.working_dir / "reviewed.txt").write_text(
            "writer final edit\n", encoding="utf-8"
        )
        self.finish_implementation(session_dir)
        ready.touch()
        try:
            wait_until(lambda: any(
                event.get("event_type") == "pass_started" and event.get("pass_kind") == "review"
                for event in self.pass_events("early-writer-edit")
            ), self.history_dir / "runs")
        except TimeoutError:
            process.kill()
            _ = process.communicate(timeout=300)
            raise
        finish_review.touch()
        _stdout, errors = process.communicate(timeout=300)

        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(
            (session_dir / "review_status_adversary").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        calls = [cast(list[str], json.loads(line)) for line in
                 (self.root / "mesh_runs_calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call[:5], ["start", "--session-dir", str(session_dir), "--to", worker])
        self.assertEqual(call[call.index("--label") + 1], "final diff ready")
        stage_two = Path(call[call.index("--done-message-file") + 1]).read_text(encoding="utf-8")
        self.assertIn(f"Stage 2: the final diff is at {session_dir / 'final_diff_1.diff'}", stage_two)
        self.assertIn(str(ready), call)

    def test_a_assigned_early_review_before_delivery_records_no_pass(self) -> None:
        session_dir = self.start_implementation("assigned-early")
        worker = self.open_worker(session_dir)
        ready = session_dir / "final_diff_1.ready"

        result = subprocess.run(
            self.review_command(session_dir, "adversary", str(ready), worker),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "review_status_adversary").read_text(encoding="utf-8"),
            "reviewed\n",
        )
        recorded = self.pass_events("assigned-early")
        self.assertEqual(
            [(event["event_type"], event.get("pass_kind")) for event in recorded],
            [("pass_started", "impl")],
        )
        heartbeat = (session_dir / "heartbeat.log").read_text(encoding="utf-8")
        self.assertIn("finished before delivery", heartbeat)

    def test_a_assigned_reviewer_that_edits_fails_and_names_the_worker_and_path(self) -> None:
        session_dir = self.start_implementation("edited")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)
        edited = self.working_dir / "reviewed.txt"

        result = self.run_assigned_review(session_dir, worker, edit_path=str(edited))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(worker, result.stderr)
        self.assertIn("reviewed.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        reviews = [
            event for event in self.pass_events("edited")
            if event.get("pass_kind") == "review"
        ]
        self.assertEqual(reviews[-1].get("status"), "error")

    def test_a_reviewer_commit_is_detected_and_names_its_changed_path(self) -> None:
        session_dir = self.start_implementation("committed-edit")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)

        result = self.run_assigned_review(
            session_dir,
            worker,
            edit_path=str(self.working_dir / "reviewed.txt"),
            commit_change=True,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(worker, result.stderr)
        self.assertIn("reviewed.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )

    def test_a_change_to_an_existing_untracked_file_is_detected(self) -> None:
        session_dir = self.start_implementation("untracked-edit")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)
        untracked = self.working_dir / "notes.txt"
        _ = untracked.write_text("before review\n", encoding="utf-8")

        result = self.run_assigned_review(
            session_dir, worker, edit_path=str(untracked)
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(worker, result.stderr)
        self.assertIn("notes.txt", result.stderr)
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "error\n"
        )

    def test_without_a_named_worker_the_fresh_read_only_path_is_unchanged(self) -> None:
        session_dir = self.start_implementation("fresh")
        self.finish_implementation(session_dir)

        result = subprocess.run(
            self.review_command(session_dir, "", ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session_dir / "mesh_events.txt").exists())
        self.assertEqual(
            (session_dir / "review_findings_1.txt").read_text(encoding="utf-8"),
            "No findings.\n",
        )
        self.assertEqual(
            (session_dir / "review_status").read_text(encoding="utf-8"), "reviewed\n"
        )

    def test_a_busy_named_worker_is_refused_before_review_artifacts_change(self) -> None:
        session_dir = self.start_implementation("busy")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, status="running")

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"codex_mesh: {worker}: worker is busy or not followable", result.stderr)
        self.assertFalse((session_dir / "review_status").exists())
        self.assertFalse((session_dir / "review_findings_1.txt").exists())

    def test_a_large_named_worker_is_compacted_before_the_review_claim(self) -> None:
        session_dir = self.start_implementation("compact")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact", "can-follow", "follow"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"compacted {worker}: 100001 -> 12000 tokens", board)

    def test_review_of_an_unmeasured_worker_does_not_compact_again(self) -> None:
        session_dir = self.start_implementation("unmeasured-review")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir)
        path = session_dir / "mesh_roster.json"
        roster = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        roster[worker]["context_tokens"] = "not_measured_since_compaction"
        _ = path.write_text(json.dumps(roster), encoding="utf-8")

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["can-follow", "follow"])
        stored = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(stored[worker]["context_tokens"], "not_measured_since_compaction")

    def test_review_posts_the_unmeasured_compaction_line_unchanged(self) -> None:
        session_dir = self.start_implementation("compact-unmeasured-line")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)
        (session_dir / "compact_reports_zero").touch()

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["compact", "can-follow", "follow"])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"launcher: compacted {worker}: 100001 tokens, size not measured until its next turn", board)

    def test_a_worker_left_with_a_live_turn_by_compaction_gets_no_review_follow_up(self) -> None:
        session_dir = self.start_implementation("compact-still-live")
        self.finish_implementation(session_dir)
        worker = self.open_worker(session_dir, context_tokens=100001)
        (session_dir / "compact_still_live").touch()

        result = self.run_assigned_review(session_dir, worker)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("still has a live turn after compaction; end the worker", result.stderr)
        self.assertEqual(
            (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
            ["compact"],
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)

    def test_both_lenses_run_at_once_without_overwriting_each_other(self) -> None:
        """The whole reason the lens exists: two reviewers, one session dir.

        Unsuffixed artifacts and a shared pass slot made the reviewers of
        a broad review destroy each other's work -- the last launched owned
        every file, and each start-pass closed the one before it as interrupted,
        leaving the ledger describing whichever happened to finish last.
        """
        session_dir = self.start_implementation("lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        lenses = ("adversary", "contract")

        running = [
            subprocess.Popen(
                self.review_command(session_dir, lens, ""),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(),
            )
            for lens in lenses
        ]
        for process in running:
            _, errors = process.communicate(timeout=300)
            self.assertEqual(process.returncode, 0, errors)

        for lens in lenses:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            self.assertTrue((session_dir / f"review_agent_1_{lens}.log").is_file())

        recorded = [
            (event["event_type"], event.get("pass_kind"), event.get("team_slot"))
            for event in self.pass_events("lenses")
        ]
        self.assertEqual(
            sorted(entry for entry in recorded if entry[1] == "review"),
            sorted(
                [
                    ("pass_started", "review", slot)
                    for slot in ("impl", "test")
                ]
                + [
                    ("pass_finished", "review", slot)
                    for slot in ("impl", "test")
                ]
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in self.pass_events("lenses")],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for slot in ("impl", "test"):
            self.assertIn(f"[{slot}] register:", board)

    def test_three_lenses_run_at_once_each_in_a_worker_of_its_own(self) -> None:
        """The craft lens joins the other two in the `review` worker.

        No writer sits there, so its pass is a third record beside the two
        lenses' and closes neither of them.
        """
        session_dir = self.start_implementation("three-lenses")
        self.recorder("finish-pass", "--session-dir", str(session_dir), "--status", "completed")
        workers = {"adversary": "test", "contract": "impl", "craft": "review"}

        running = [
            subprocess.Popen(
                self.review_command(session_dir, lens, ""),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.environment(),
            )
            for lens in workers
        ]
        for process in running:
            _, errors = process.communicate(timeout=300)
            self.assertEqual(process.returncode, 0, errors)

        for lens in workers:
            self.assertEqual(
                (session_dir / f"review_status_{lens}").read_text().strip(), "reviewed"
            )
            self.assertTrue((session_dir / f"review_findings_1_{lens}.txt").is_file())
            self.assertTrue((session_dir / f"review_agent_1_{lens}.log").is_file())

        events = self.pass_events("three-lenses")
        reviews = [event for event in events if event.get("pass_kind") == "review"]
        self.assertEqual(
            sorted((str(event["event_type"]), str(event.get("team_slot"))) for event in reviews),
            sorted(
                (event_type, slot)
                for event_type in ("pass_started", "pass_finished")
                for slot in workers.values()
            ),
        )
        self.assertNotIn(
            "interrupted",
            [str(event.get("status")) for event in events],
        )

        board = (session_dir / "board.log").read_text(encoding="utf-8")
        for lens, slot in workers.items():
            self.assertIn(f"[{slot}] register: {lens} review up", board)

    def test_an_unknown_lens_is_refused_before_anything_runs(self) -> None:
        session_dir = self.start_implementation("unknown-lens")
        result = subprocess.run(
            self.review_command(session_dir, "style", ""),
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unknown review lens 'style' (adversary, contract, craft, ux)", result.stderr)
        self.assertFalse((session_dir / "review_status_style").exists())
        self.assertEqual(
            [event["event_type"] for event in self.pass_events("unknown-lens")],
            ["pass_started"],
        )

    def test_the_wrapper_names_a_verdict_it_did_not_record(self) -> None:
        session_dir = self.start_implementation("void")
        result = self.run_review(session_dir, session_dir / "final_diff_1.ready")
        self.assertEqual(result.returncode, 0, result.stderr)
        heartbeat = (session_dir / "heartbeat.log").read_text(encoding="utf-8")
        self.assertIn("finished before delivery", heartbeat)
        self.assertIn("verdict void", heartbeat)


if __name__ == "__main__":
    _ = unittest.main()
