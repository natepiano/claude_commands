"""Integration tests for review.sh's fresh and long-lived reviewer paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict, ClassVar, cast, override

from scripts.direct.recorder_fixture import communicate_launcher, invoke_recorder
from scripts.direct.implement_launcher_test_support import copy_launcher_dependencies


TEST_HANG_GUARD_SECS = 300  # load 136 on natedev, 2026-10-29

DIRECT_DIR = Path(__file__).parent

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
import fcntl
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

def save_entry():
    with (session / 'fixture_workers.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(roster_path.read_text()) if roster_path.exists() else {}
        current[name] = entry
        roster_path.write_text(json.dumps(current))

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
    save_entry()
    if (session / 'compact_reports_zero').exists():
        print(f'compacted {name}: {before} tokens, size not measured until its next turn')
    else:
        print(f'compacted {name}: {before} -> 12000 tokens')
elif verb == 'can-follow':
    same_claim = (entry.get('status') == 'starting'
                  and entry.get('launcher_pid') == int(option('--claim-pid')))
    if not same_claim and entry.get('status') not in ('done', 'failed', 'paused'):
        print(f'codex_mesh: {name}: worker is busy or not followable; follow refused',
              file=sys.stderr)
        sys.exit(2)
    if not same_claim:
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
    save_entry()
    if os.environ.get('TEST_WORKER_STARTED'):
        pathlib.Path(os.environ['TEST_WORKER_STARTED']).touch()
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
        entry['status'] = 'failed'
        save_entry()
        sys.exit(1)
else:
    sys.exit(2)
save_entry()
if verb == 'follow' and os.environ.get('STUB_REVIEW_DONE_FILE'):
    pathlib.Path(os.environ['STUB_REVIEW_DONE_FILE']).touch()
"""


STUB_WORKERS = r'''#!/usr/bin/env python3
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

run = Path(sys.argv[sys.argv.index("--session-dir") + 1])
state = json.loads((run / "progress_history_state.json").read_text())
cwd = state["working_dir"]
name = subprocess.check_output(["bash", str(Path(__file__).with_name("worker_name.sh")), cwd, "review", "review"], text=True).strip()
owner_pid = int(sys.argv[sys.argv.index("--owner-pid") + 1])

def occupied(record):
    pid = record.get("launcher_pid", 0)
    if pid:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            pass
    return record.get("status") not in ("done", "failed", "paused", "starting")

with (run / "fixture_workers.lock").open("a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = run / "mesh_roster.json"
    records = json.loads(path.read_text()) if path.exists() else {}
    base = name
    number = 1
    while name in records and occupied(records[name]):
        number += 1
        name = f"{base}{number}"
    if name not in records:
        records[name] = {"thread_id": "thread-" + name, "status": "done", "role": "review", "context_tokens": 0}
    entry = records[name]
    entry.update({"previous_status": entry.get("previous_status", entry["status"]),
                  "previous_role": entry.get("role", "review"),
                  "status": "starting", "launcher_pid": owner_pid})
    path.write_text(json.dumps(records))
    print(name)
'''


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


class ReviewLauncherFixture(unittest.TestCase):
    """The early-launch contract: before the sentinel, no pass is recorded.

    The reviewer starts while the writer is still working, so the
    implementation pass is open the whole time it runs. A start-pass before the
    sentinel files a review pass for a verdict that cannot have read the final
    diff, and does it under the reviewer's own slot, where nothing later
    reconciles it against the diff it never saw.
    """

    fixture_seed: ClassVar[Path]

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    review_script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    prompt_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        seed_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(seed_directory.cleanup)
        cls.fixture_seed = Path(seed_directory.name)
        seed = cls("runTest")
        seed.root = cls.fixture_seed
        seed.prepare_fixture()

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # Private files and Git objects/index/refs: mutations never reach another test.
        _ = shutil.copytree(self.fixture_seed, self.root, dirs_exist_ok=True)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "project"
        self.config_file = self.root / "direct.conf"
        self.prompt_file = self.root / "review_prompt.md"
        self.review_script = self.root / "scripts" / "direct" / "review.sh"

    def prepare_fixture(self) -> None:
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
        copy_launcher_dependencies(self.root)
        direct_dir = self.root / "scripts" / "direct"
        agents = self.root / "scripts" / "agents"
        lib = self.root / "scripts" / "lib"
        production = self.root / "scripts" / "production"
        _ = shutil.copy2(DIRECT_DIR.parent / "production" / "showrunners.py", production / "showrunners.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "probe_session.py", lib / "probe_session.py")
        for name in ("review.sh", "review_tree.py", "tag_inventory.py"):
            _ = shutil.copy2(DIRECT_DIR / name, direct_dir / name)
        launcher = direct_dir / "review.sh"
        _ = launcher.write_text(
            launcher.read_text(encoding="utf-8").replace(
                "HEARTBEAT_INTERVAL_SECS=60", "HEARTBEAT_INTERVAL_SECS=1"
            ), encoding="utf-8",
        )
        stub = agents / "agent_exec.sh"
        _ = stub.write_text(STUB_AGENT_EXEC, encoding="utf-8")
        stub.chmod(0o755)
        mesh = agents / "codex_mesh.py"
        _ = mesh.write_text(STUB_MESH, encoding="utf-8")
        mesh.chmod(0o755)
        _ = (direct_dir / "review_pool.py").write_text(STUB_POOL, encoding="utf-8")
        _ = (direct_dir / "workers.py").write_text(STUB_WORKERS, encoding="utf-8")
        return direct_dir / "review.sh"

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.root)
        environment["XDG_CACHE_HOME"] = str(self.root / "cache")
        cache = self.root / "cache" / "claude" / "python3-3.11"
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
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )

    def finish_early_launcher(self, process: subprocess.Popen[str], ready: Path,
                              finish: Path, timeout: float = TEST_HANG_GUARD_SECS) -> tuple[str, str]:
        ready.touch()
        finish.touch()
        return communicate_launcher(process, timeout)

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
            timeout=TEST_HANG_GUARD_SECS,
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
            timeout=TEST_HANG_GUARD_SECS,
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
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        return result, session / "phases" / "3"
