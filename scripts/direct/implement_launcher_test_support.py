"""Integration tests for implement.sh's worker arguments.

The failure these exist for left no error anywhere. A repair round went out with
the pass kind on one worker, the other two launchers ran their agents normally,
and the ledger kept describing the round before it -- two records closed `error`
an hour earlier -- until the one live window closed and the recorder began
refusing every progress call while two agents were still working.
"""

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


TEST_HANG_GUARD_SECS = 300  # load 136 on natedev, 2026-10-29

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

# The wrapper calls this as: <task> write <working_dir> <prompt> <reply> <log>.
# Writing both outputs and exiting immediately drives the wrapper's completion
# branch without a model call; the wrapper copies the reply into the summary
# file because the stub, like a worker that skipped its last act, left it
# empty.
STUB_AGENT_EXEC = """#!/usr/bin/env bash
set -euo pipefail
printf 'Wrote the retry path.\\n' > "$5"
printf 'stub agent log\\n' > "$6"
if [[ -n "${TEST_WORKER_STARTED:-}" ]]; then : > "$TEST_WORKER_STARTED"; fi
"""

STUB_MESH = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time
import signal

verb = sys.argv[1]
def option(name):
    return sys.argv[sys.argv.index(name) + 1]

session = pathlib.Path(option('--session-dir'))
roster_path = session / 'mesh_roster.json'
roster = json.loads(roster_path.read_text()) if roster_path.exists() else {}
if verb in ('compact', 'can-follow', 'follow'):
    with (session / 'mesh_events.txt').open('a') as events:
        events.write(verb + '\\n')
if verb == 'follow':
    (session / 'follow_cwd.txt').write_text(option('--cwd'))
if verb == 'start':
    name = option('--name')
    roster[name] = {'thread_id': 'thread-for-' + name, 'turn_id': 'turn-1',
                    'status': 'done', 'role': option('--role'), 'task': option('--task')}
    pathlib.Path(option('--reply-file')).write_text('initial reply\\n')
elif verb == 'compact':
    name = option('--to')
    if os.environ.get('STUB_COMPACT_FAIL') == '1':
        print('stub compaction failed', file=sys.stderr)
        sys.exit(1)
    if os.environ.get('STUB_COMPACT_FAIL') == '3':
        print('stub turn still live', file=sys.stderr)
        sys.exit(3)
    before = roster[name].get('context_tokens', 0)
    after = int(os.environ.get('STUB_COMPACT_AFTER', '12000'))
    roster[name]['context_tokens'] = ('not_measured_since_compaction'
                                    if (session / 'compact_reports_zero').exists() else after)
    roster_path.write_text(json.dumps(roster))
    if (session / 'compact_reports_zero').exists():
        print(f'compacted {name}: {before} tokens, size not measured until its next turn')
    else:
        print(f'compacted {name}: {before} -> {after} tokens')
elif verb == 'can-follow':
    status_file = session / 'impl_status_impl'
    if status_file.exists():
        (session / 'status_before_claim.txt').write_text(status_file.read_text())
    name = option('--to')
    entry = roster.get(name, {})
    status = entry.get('status')
    pid = entry.get('launcher_pid', 0)
    alive = False
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            pass
    if status not in ('done', 'failed', 'paused') and not (
        status in ('running', 'starting') and pid and not alive
    ):
        sys.exit(2)
    if entry.get('live_turn'):
        sys.exit(2)
    entry['previous_status'] = status if status in ('done', 'failed') else 'failed'
    entry['status'] = 'starting'
    entry['role'] = option('--role')
    entry['launcher_pid'] = int(option('--claim-pid'))
    roster_path.write_text(json.dumps(roster))
    sys.exit(0)
elif verb == 'follow':
    name = option('--to')
    if roster[name].get('status') != 'starting' or roster[name].get('launcher_pid') != int(option('--claim-pid')):
        sys.exit(2)
    if os.environ.get('STUB_FOLLOW_PEER_AFTER_CLAIM') == '1':
        roster[name] = {'thread_id': roster[name]['thread_id'], 'turn_id': 'peer-turn',
                        'status': 'running'}
        roster_path.write_text(json.dumps(roster))
        sys.exit(1)
    if os.environ.get('STUB_FOLLOW_HOLD') == 'before':
        signal.pause()
    roster[name]['role'] = option('--role')
    roster[name]['task'] = option('--task')
    (session / 'follow_message.txt').write_text(pathlib.Path(option('--message-file')).read_text())
    if os.environ.get('STUB_FOLLOW_FAIL') == '1':
        roster[name]['status'] = 'failed'
        roster_path.write_text(json.dumps(roster))
        sys.exit(1)
    roster[name]['turn_id'] = 'turn-2'
    roster[name]['status'] = 'running' if os.environ.get('STUB_FOLLOW_HOLD') == 'after' else 'done'
    if os.environ.get('STUB_FOLLOW_HOLD') == 'after':
        roster[name]['launcher_pid'] = os.getpid()
    roster_path.write_text(json.dumps(roster))
    if os.environ.get('STUB_FOLLOW_HOLD') == 'after':
        if os.environ.get('STUB_LAUNCH_RELEASE'):
            import subprocess
            subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], os.environ['STUB_LAUNCH_RELEASE']], check=True)
        else:
            signal.pause()
        roster[name]['status'] = 'done'
    pathlib.Path(option('--summary-file')).write_text('follow-up summary\\n')
elif verb == 'release-follow':
    name = option('--to')
    entry = roster.get(name, {})
    if entry.get('status') == 'starting' and entry.get('launcher_pid') == int(option('--claim-pid')):
        roster[name] = {'thread_id': entry['thread_id'],
                        'status': entry.get('previous_status', 'failed')}
else:
    sys.exit(2)
roster_path.write_text(json.dumps(roster))
if verb == 'start' and os.environ.get('STUB_LAUNCH_RELEASE'):
    import subprocess
    subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], os.environ['STUB_LAUNCH_RELEASE']], check=True)
"""

CLAUDE_REGISTRY = """[assignments]
direct=claude

[direct.claude]
impl=opus:high
test=opus:high

[claude.agents]
opus=low,medium,high,xhigh,max
"""

STUB_CLAUDE = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ['STUB_CLAUDE_ROOT'])
if sys.argv[1] == '--bg':
    (root / 'launch_args.json').write_text(json.dumps(sys.argv[1:]))
    (root / 'launch_environment.json').write_text(json.dumps({
        key: os.environ.get(key, '')
        for key in ('CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_REMOTE_MEMORY_DIR')
    }))
    (root / 'worker_state').write_text('busy')
    own_summary = root / 'own_summary.bin'
    if own_summary.exists():
        pathlib.Path(os.environ['STUB_SUMMARY']).write_bytes(own_summary.read_bytes())
    name = sys.argv[sys.argv.index('--name') + 1]
    import uuid
    registry = pathlib.Path(os.environ.get('NOTIFIER_SESSIONS_DIR', str(root / 'sessions')))
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f'{name}.json').write_text(json.dumps({
        'id': 'abc12345', 'sessionId': str(uuid.uuid5(uuid.NAMESPACE_URL, name)), 'name': name}))
    print(f'backgrounded · abc12345 · {name}')
elif sys.argv[1:3] == ['agents', '--json']:
    unreadable = root / 'unreadable_listing_once'
    if unreadable.exists():
        unreadable.unlink()
        print('not JSON')
        sys.exit(0)
    status = (root / 'worker_state').read_text().strip()
    if status == 'busy':
        (root / 'worker_state').write_text('idle')
    launch_args = root / 'launch_args.json'
    name = 'fixture-impl'
    if launch_args.exists():
        arguments = json.loads(launch_args.read_text())
        name = arguments[arguments.index('--name') + 1]
    print(json.dumps([{'id': 'abc12345', 'name': name, 'status': status}]
                     if status != 'gone' else []))
elif sys.argv[1] == 'logs':
    pane = root / 'pane.bin'
    sys.stdout.buffer.write(pane.read_bytes() if pane.exists() else b'stub Claude log\\n')
else:
    sys.exit(2)
"""

STUB_SEND = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ['STUB_CLAUDE_ROOT'])
(root / 'delivered_message.txt').write_text(pathlib.Path(sys.argv[sys.argv.index('--file') + 1]).read_text())
behavior = os.environ.get('STUB_SEND_BEHAVIOR', 'complete')
if behavior == 'queued':
    print('QUEUED')
    sys.exit(0)
transcript = pathlib.Path(os.environ['STUB_TRANSCRIPT'])
with transcript.open('a') as output:
    output.write(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'end_turn'}}) + '\\n')
pathlib.Path(os.environ['STUB_SUMMARY']).write_text('Claude follow-up summary\\n')
if behavior == 'gone':
    (root / 'worker_state').write_text('gone\\n')
if behavior == 'unreadable_once':
    (root / 'unreadable_listing_once').write_text('yes\\n')
print('DELIVERED')
"""


class OpenPass(TypedDict):
    worker_name: str
    kind: str
    status: str


# "pass" is a keyword, so the recorder's state key needs the functional form.
ProgressState = TypedDict("ProgressState", {"pass": dict[str, OpenPass]})


AGENTS_REGISTRY = """[assignments]
direct=codex

[direct.codex]
impl=gpt-called:xhigh
test=gpt-called:xhigh
fix=gpt-called:xhigh
review=gpt-blind:max

[codex.agents]
gpt-called=low,medium,high,xhigh,max
gpt-blind=low,medium,high,xhigh,max
"""


class ImplementLauncherFixture(unittest.TestCase):
    """Every worker carries a kind, and the board line says which."""

    fixture_seed: ClassVar[Path]

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    implement_script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
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
        self.prompt_file = self.root / "implementation_prompt.md"
        self.implement_script = self.root / "scripts" / "direct" / "implement.sh"

    def prepare_fixture(self) -> None:
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "project"
        self.working_dir.mkdir()
        _ = subprocess.run(
            ["git", "init", "-b", "feature/workers"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        self.config_file = self.root / "direct.conf"
        _ = self.config_file.write_text(
            "\n".join((
                "UNIT_PROGRESS_INTERVAL_SECONDS=180",
                "UNIT_COMPACT_ABOVE_TOKENS=100000",
                "MAX_FIX_ATTEMPTS=3", "MAX_REOPENS=3", "STALLED_ROUNDS=3",
                "RUNAWAY_ROUNDS=10", "REPAIR_ROUNDS_PER_FINDING=2",
                "MIN_REPAIR_BUDGET=2", "MAX_CONSECUTIVE_SAME_KIND_PASSES=6",
                "MAX_REVIEW_CANCELLATIONS=1", "ROUTING_FROM_ROUND=4",
                "ASTRA_AFTER_FAILED_FIXES=2", "",
            )),
            encoding="utf-8",
        )
        registry = self.root / ".claude" / "config" / "agents.conf"
        registry.parent.mkdir(parents=True)
        _ = registry.write_text(AGENTS_REGISTRY, encoding="utf-8")
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude2.md").write_text("---\nlogin: worker@example.invalid\n---\n")
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "worker@example.invalid"},
        }))
        self.prompt_file = self.root / "implementation_prompt.md"
        _ = self.prompt_file.write_text("Write the retry path.\n", encoding="utf-8")
        self.implement_script = self.build_script_tree()

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_script_tree(self) -> Path:
        """Copy the real wrapper beside a stub agent, so nothing calls a model."""
        direct_dir = self.root / "scripts" / "direct"
        agents = self.root / "scripts" / "agents"
        lib = self.root / "scripts" / "lib"
        direct_dir.mkdir(parents=True)
        agents.mkdir(parents=True)
        lib.mkdir(parents=True)
        production = self.root / "scripts" / "production"
        production.mkdir(parents=True)
        _ = shutil.copy2(DIRECT_DIR.parent / "production" / "production_doc.py", production / "production_doc.py")
        # The wrapper reaches python3 through ../lib/py, resolved from its own
        # location, so the copy needs the interpreter shim beside it too.
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "py", lib / "py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "nix_env.py", lib / "nix_env.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "user_zone.py", lib / "user_zone.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "unit_runs.py", lib / "unit_runs.py")
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "agents_registry.py", lib / "agents_registry.py")
        home_lib = self.root / ".claude" / "scripts" / "lib"
        home_lib.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(DIRECT_DIR.parent / "lib" / "py", home_lib / "py")
        for name in ("implement.sh", "worker_name.sh", "worker_state.py", "progress_history.py", "plan_history.py", "phase_reservation.py",
                     "recorder_state.py", "findings.py", "board.sh"):
            _ = shutil.copy2(DIRECT_DIR / name, direct_dir / name)
        recorder = direct_dir / "progress_history.py"
        _ = recorder.write_text(recorder.read_text().replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n" + LEGACY_RECORDER_ARGUMENTS, 1,
        ))
        launcher = direct_dir / "implement.sh"
        _ = launcher.write_text(
            launcher.read_text(encoding="utf-8").replace(
                "HEARTBEAT_INTERVAL_SECS=60", "HEARTBEAT_INTERVAL_SECS=1"
            ), encoding="utf-8",
        )
        for name in ("agents_config.sh", "agents_overlay.sh", "agents_overlay.awk", "claude_account.sh", "heartbeat.sh", "heartbeat_watch.sh"):
            _ = shutil.copy2(AGENTS_DIR / name, agents / name)
        stub = agents / "agent_exec.sh"
        _ = stub.write_text(STUB_AGENT_EXEC, encoding="utf-8")
        stub.chmod(0o755)
        mesh = agents / "codex_mesh.py"
        _ = mesh.write_text(STUB_MESH, encoding="utf-8")
        mesh.chmod(0o755)
        agent_bg = agents / "agent_bg.sh"
        _ = shutil.copy2(AGENTS_DIR / "agent_bg.sh", agent_bg)
        _ = agent_bg.write_text(
            agent_bg.read_text(encoding="utf-8").replace(
                'POLL_SECS="${8:-15}"', 'POLL_SECS="${8:-0.05}"'
            ), encoding="utf-8",
        )
        return direct_dir / "implement.sh"

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.root)
        environment["AGENT_NOTES_DIR"] = str(self.root / "notes")
        # The copied shell helper uses the real account policy, while identity
        # and configuration stay in this fixture's home.
        environment["PYTHONPATH"] = str(DIRECT_DIR.resolve().parents[1])
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                     "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CODEX_HOME"):
            _ = environment.pop(name, None)
        environment["XDG_CACHE_HOME"] = str(self.root / "cache")
        cache = self.root / "cache" / "claude" / "python3-3.11"
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(sys.executable + "\n")
        _ = environment.pop("CLAUDE_PYTHON", None)
        environment["UNIT_WORKER_POLL_SECS"] = "0.01"
        environment["UNIT_WORKER_WAIT_ATTEMPTS"] = "30000"
        environment["UNIT_SYNC_WRITES"] = "0"
        environment["TEST_WAIT_HELPER"] = str(DIRECT_DIR.resolve() / "wait_for_test_state.py")
        environment["TEST_HEARTBEAT_READY"] = str(self.root / "heartbeat-ready")
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["TEST_LEGACY_LAUNCH"] = "1"
        environment["UNIT_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.root / ".claude" / "config" / "agents.conf")
        # The plain launcher, so the stub above stands in for the agent. The mesh
        # path would need a live app-server, which is a different test's subject.
        environment["UNIT_CODEX_MESH"] = "0"
        environment["TZ"] = "UTC"
        environment["USER_ZONE"] = "UTC"
        _ = environment.pop("CODEX_THREAD_ID", None)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
        return environment

    def recorder(self, *arguments: str) -> None:
        environment = self.environment()
        environment["UNIT_PASS_OWNER"] = "launcher"
        result = invoke_recorder(arguments, environment, Path.cwd())
        self.assertEqual(result.returncode, 0, result.stderr)

    def start_phase(self, name: str) -> Path:
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
        return session_dir

    def launch(
        self, session_dir: Path, kind: str, slot: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash", str(self.implement_script), str(session_dir), str(self.working_dir),
                str(self.prompt_file), kind, "the retry path", kind,
                "writing the retry path", "0", slot,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )

    def checked_launch(
        self, session: Path, *options: str, kind: str = "impl",
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        _ = environment.pop("TEST_LEGACY_LAUNCH", None)
        environment["TEST_WORKER_STARTED"] = str(session / "worker-started")
        return subprocess.run(
            ["bash", str(self.implement_script), *options, str(session), str(self.working_dir),
             str(self.prompt_file), kind, "the retry path", kind, "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )

    def launch_records(self, session: Path, kind: str) -> list[dict[str, object]]:
        path = self.history_dir / "runs" / f"{session.name}.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()
                if cast(dict[str, object], json.loads(line)).get("event_type") == kind]

    def claim_fixture(self, session: Path, status: str) -> tuple[dict[str, str], Path]:
        """Phase 3 awaits its first touch over `reviewed.txt`; a stub cargo-berth answers `status`."""
        phase_dir = session / "phases" / "3"
        phase_dir.mkdir(parents=True, exist_ok=True)
        _ = (phase_dir / "delegated_phase_reservation_state.json").write_text(
            json.dumps({"kind": "enrolled_awaiting_first_touch", "phase": "3"}), encoding="utf-8")
        _ = (phase_dir / "files").write_text("reviewed.txt\nsrc/\n", encoding="utf-8")
        binaries = session / "claim-bin"
        binaries.mkdir()
        calls = session / "berth-calls.txt"
        answer = json.dumps({"status": status, "message": "held by another run" if status != "clear" else "",
                             "payload": {"data": {"acquisition": {
                                 "reservation_id": "r1", "coordination_run_id": "c1", "phase_start_head": "h1"}}}})
        berth = binaries / "cargo-berth"
        _ = berth.write_text(f"""#!/usr/bin/env bash
printf '%s %s\\n' "$CARGO_BERTH_SESSION_ID" "$*" >> {calls}
printf 'claim\\n' >> {session / "mesh_events.txt"}
printf '%s\\n' '{answer}'
""", encoding="utf-8")
        berth.chmod(0o755)
        environment = self.mesh_environment()
        environment["PATH"] = str(binaries) + os.pathsep + environment["PATH"]
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        return environment, calls

    def mesh_environment(self, fail: bool = False) -> dict[str, str]:
        environment = self.environment()
        environment["UNIT_CODEX_MESH"] = "1"
        if fail:
            environment["STUB_FOLLOW_FAIL"] = "1"
        return environment

    def finish_default_launcher(self, process: subprocess.Popen[str], release: Path,
                                mesh: Path, timeout: float = TEST_HANG_GUARD_SECS) -> tuple[str, str]:
        try:
            release.touch()
            return communicate_launcher(process, timeout)
        finally:
            # A timeout must also restore the fixture for the next subcase.
            _ = mesh.write_text(STUB_MESH)

    def mesh_launch(
        self, session_dir: Path, *, to: str = "", slot: str = "impl",
        kind: str = "impl", fail: bool = False, resolves_round: bool = False,
        compact_fail: bool = False, subtask: str = "", compact_still_live: bool = False,
        fix_cause: str = "",
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            "bash", str(self.implement_script),
            str(session_dir), str(self.working_dir), str(self.prompt_file),
            subtask or kind, "the retry path", kind, "writing the retry path", "0", slot,
        ]
        if fix_cause:
            arguments[2:2] = ["--fix-cause", fix_cause]
        if to:
            arguments.insert(2, "--to")
            arguments.insert(3, to)
        environment = self.mesh_environment(fail=fail)
        if compact_fail:
            environment["STUB_COMPACT_FAIL"] = "1"
        if compact_still_live:
            environment["STUB_COMPACT_FAIL"] = "3"
        if resolves_round:
            environment["UNIT_RESOLVES_ROUND"] = "1"
        return subprocess.run(
            arguments, check=False, capture_output=True, text=True,
            env=environment, timeout=TEST_HANG_GUARD_SECS,
        )

    def set_mesh_context(self, session_dir: Path, name: str, tokens: int) -> None:
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        roster[name]["context_tokens"] = tokens
        _ = roster_path.write_text(json.dumps(roster), encoding="utf-8")

    def open_repair_round(self, session_dir: Path) -> None:
        findings = self.root / "scripts" / "direct" / "findings.py"
        for arguments in (
            ("open", "--severity", "blocker", "--title", "Repair retry",
             "--caught-by", "worker"),
            ("dispatch", "--covers", "F001", "--accepted-checks", "Retry preserves queued data"),
        ):
            result = subprocess.run(
                ["python3", str(findings), arguments[0], "--session-dir", str(session_dir),
                 *arguments[1:]],
                check=False, capture_output=True, text=True, env=self.environment(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def repair_outcome(self, session_dir: Path) -> str:
        state = cast("dict[str, object]", json.loads(
            (session_dir / "findings_state.json").read_text(encoding="utf-8")
        ))
        rounds = cast("list[dict[str, object]]", state["rounds"])
        return cast("str", rounds[-1]["outcome"])

    def observe_status_at_records(self, *, fail_landed: bool = False) -> None:
        """Record the status each copied helper sees before it updates its ledger."""
        direct_dir = self.root / "scripts" / "direct"
        for name, commands in (
            ("progress_history.py", ("finish-pass",)),
            ("findings.py", ("landed", "abandon")),
        ):
            script = direct_dir / name
            probe = f"""import pathlib as _probe_pathlib
import sys as _probe_sys
if len(_probe_sys.argv) > 1 and _probe_sys.argv[1] in {commands!r}:
    _probe_session = _probe_pathlib.Path(
        _probe_sys.argv[_probe_sys.argv.index('--session-dir') + 1]
    )
    _probe_status = (_probe_session / 'impl_status_impl').read_text(encoding='utf-8')
    (_probe_session / ('observed_' + _probe_sys.argv[1] + '.txt')).write_text(
        _probe_status, encoding='utf-8'
    )
    if _probe_sys.argv[1] == 'landed' and {fail_landed!r}:
        _probe_sys.exit(17)
"""
            original = script.read_text(encoding="utf-8")
            _ = script.write_text(original.replace(
                "from __future__ import annotations\n",
                "from __future__ import annotations\n" + probe,
                1,
            ), encoding="utf-8")

    def observed_status(self, session_dir: Path, command: str) -> str:
        return (session_dir / f"observed_{command}.txt").read_text(encoding="utf-8")

    def claude_worker(self, session_dir: Path) -> Path:
        registry = self.root / ".claude" / "config" / "agents.conf"
        _ = registry.write_text(CLAUDE_REGISTRY, encoding="utf-8")
        sessions = self.root / "sessions"
        sessions.mkdir()
        projects = self.root / "projects" / "fixture"
        projects.mkdir(parents=True)
        session_id = "11111111-2222-3333-4444-555555555555"
        _ = (sessions / "12345.json").write_text(json.dumps({
            "id": "abc12345", "sessionId": session_id, "name": "fixture-impl",
        }), encoding="utf-8")
        transcript = projects / f"{session_id}.jsonl"
        _ = transcript.write_text(
            json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn"}})
            + "\n", encoding="utf-8"
        )
        _ = (session_dir / "workers").write_text(
            "abc12345\tfixture-impl\n", encoding="utf-8"
        )
        _ = (session_dir / "impl_bg_id_impl").write_text(
            "abc12345\n", encoding="utf-8"
        )
        _ = (session_dir / "impl_worker_impl.json").write_text(json.dumps({
            "name": "fixture-impl", "slot": "impl", "family": "claude",
            "model": "opus", "worker_id": session_id,
        }), encoding="utf-8")
        _ = (session_dir / "impl_status_impl").write_text(
            "implemented\n", encoding="utf-8"
        )
        _ = (self.root / "worker_state").write_text("idle\n", encoding="utf-8")
        for name, content in (("claude", STUB_CLAUDE), ("send", STUB_SEND)):
            script = self.root / name
            _ = script.write_text(content, encoding="utf-8")
            script.chmod(0o755)
        return transcript

    def claude_follow(
        self, session_dir: Path, transcript: Path, behavior: str
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        environment.update({
            "CLAUDE_BIN": str(self.root / "claude"),
            "AGENT_BG_SEND_SCRIPT": str(self.root / "send"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CLAUDE_PROJECTS_DIR": str(self.root / "projects"),
            "STUB_CLAUDE_ROOT": str(self.root),
            "STUB_TRANSCRIPT": str(transcript),
            "STUB_SUMMARY": str(session_dir / "impl_summary_impl.txt"),
            "STUB_SEND_BEHAVIOR": behavior,
        })
        try:
            return subprocess.run(
                ["bash", str(self.implement_script), "--to", "fixture-impl",
                 str(session_dir), str(self.working_dir), str(self.prompt_file),
                 "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
                check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
            )
        except subprocess.TimeoutExpired as error:
            delivered = self.root / "delivered_message.txt"
            raise AssertionError(
                " ".join((
                    f"Claude follow timed out: transcript={transcript.read_text()!r},",
                    f"delivered={delivered.exists()},",
                    f"worker_state={(self.root / 'worker_state').read_text()!r},",
                    f"stdout={error.stdout!r}, stderr={error.stderr!r}",
                ))
            ) from error

    def claude_launch(
        self, session_dir: Path, registry_row: str, *, inherited_effort: str = "", kind: str = "impl"
    ) -> subprocess.CompletedProcess[str]:
        registry = self.root / ".claude" / "config" / "agents.conf"
        _ = registry.write_text(
            CLAUDE_REGISTRY.replace("impl=opus:high", f"impl={registry_row}")
            .replace(
                "opus=low,medium,high,xhigh,max",
                "opus=low,medium,high,xhigh,max\nsonnet=low,medium,high,xhigh,max",
            ),
            encoding="utf-8",
        )
        stub = self.root / "claude"
        _ = stub.write_text(STUB_CLAUDE, encoding="utf-8")
        stub.chmod(0o755)
        environment = self.environment()
        environment["CLAUDE_BIN"] = str(stub)
        environment["STUB_CLAUDE_ROOT"] = str(self.root)
        environment["NOTIFIER_SESSIONS_DIR"] = str(self.root / "sessions")
        environment["STUB_SUMMARY"] = str(session_dir / "impl_summary_impl.txt")
        if inherited_effort:
            environment["AGENT_BG_EFFORT"] = inherited_effort
        else:
            _ = environment.pop("AGENT_BG_EFFORT", None)
        return subprocess.run(
            ["bash", str(self.implement_script), str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", kind,
             "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )

    def open_passes(self, session_dir: Path) -> dict[str, OpenPass]:
        stored = cast(
            "ProgressState",
            json.loads((session_dir / "progress_history_state.json").read_text(encoding="utf-8")),
        )
        return stored["pass"]
