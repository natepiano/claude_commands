#!/usr/bin/env python3
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
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TypedDict, cast, override
from unittest.mock import patch

from scripts.accounts import accounts
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


class ImplementLauncherWorkerTests(unittest.TestCase):
    """Every worker carries a kind, and the board line says which."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    implement_script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    prompt_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
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
        cache = self.root / "cache" / "claude" / "python3-3.10"
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
            timeout=300,
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
            check=False, capture_output=True, text=True, env=environment, timeout=300,
        )

    def launch_records(self, session: Path, kind: str) -> list[dict[str, object]]:
        path = self.history_dir / "runs" / f"{session.name}.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()
                if cast(dict[str, object], json.loads(line)).get("event_type") == kind]

    def test_a_companion_fix_starts_after_the_resolver_lands_its_round(self) -> None:
        session = self.start_phase("landed-companion")
        self.open_repair_round(session)
        landed = subprocess.run(
            [sys.executable, str(DIRECT_DIR / "findings.py"), "landed",
             "--session-dir", str(session)],
            env=self.environment(), capture_output=True, text=True, check=False,
        )
        self.assertEqual(landed.returncode, 0, landed.stderr)
        result = self.checked_launch(session, "--phase", "3", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session / "worker-started").exists())
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["fix_cause"], {"round": 1, "covered": ["F001"]})

    def test_launcher_passes_its_prompt_to_the_launch_check(self) -> None:
        session = self.start_phase("checked-launch")
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nWrite the retry.\n")
        result = self.checked_launch(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual((check["phase_id"], check["phase_title"], check["prompt_phase_line"]),
                         ("3", "Retry handling", "matched"))
        self.assertTrue((session / "worker-started").exists())

    def test_launcher_stops_on_stale_prompt_and_missing_phase_refusals(self) -> None:
        for label, options, text in (
            ("stale-prompt", ("--phase", "3"), "Phase 9 — Other work\n"),
            ("missing-phase", (), "Write the retry.\n"),
        ):
            with self.subTest(label=label):
                session = self.start_phase(f"refused-{label}")
                _ = self.prompt_file.write_text(text)
                result = self.checked_launch(session, *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((session / "worker-started").exists())
                self.assertEqual(self.launch_records(session, "pass_started"), [])
                refusal, = self.launch_records(session, "launch_refused")
                self.assertEqual(refusal["prompt_file"], str(self.prompt_file))
                self.assertTrue(refusal["reason"])
                self.assertIn("prompt" if label == "stale-prompt" else "phase", result.stderr.lower())

    def test_launcher_passes_the_explicit_failed_gate_cause_for_a_fix(self) -> None:
        session = self.start_phase("checked-fix-cause")
        result = self.checked_launch(session, "--phase", "3", "--fix-cause",
                                     "retry tests fail after integration", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["fix_cause"], "retry tests fail after integration")

    def test_a_launch_naming_its_phase_writes_in_that_phase_directory(self) -> None:
        # Two phases running the same role each name their own, so neither overwrites the
        # other's status, summary or log, and the name stays off the worker's address.
        session_dir = self.start_phase("phase-directory")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--phase", "3", str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=self.environment(), timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        phase_dir = session_dir / "phases" / "3"
        self.assertEqual((phase_dir / "impl_status_impl").read_text(encoding="utf-8").strip(), "implemented")
        self.assertTrue((phase_dir / "impl_agent_impl.log").is_file())
        self.assertEqual(sorted(path.name for path in session_dir.glob("impl_status*")), [])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertNotIn("3-impl", board)

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

    def test_a_codex_seat_on_the_mac_opens_only_after_its_phase_claims_its_files(self) -> None:
        # A Mac Codex seat runs no berth check of its own, so the launcher claims the phase first.
        session = self.start_phase("claim-before-seat")
        initial = self.mesh_launch(session, slot="mac")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        seat = next(iter(cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))))
        environment, calls = self.claim_fixture(session, "clear")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", seat, "--phase", "3", str(session), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "mac"],
            check=False, capture_output=True, text=True, env=environment, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(),
                         ["sid-director.phase-3 check reviewed.txt --json"])
        state = (session / "phases" / "3" / "delegated_phase_reservation_state.json").read_text(encoding="utf-8")
        self.assertIn('"r1"', state)
        events = (session / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("follow", events[events.index("claim"):], events)

    def test_a_refused_claim_keeps_the_phases_codex_seat_closed(self) -> None:
        session = self.start_phase("claim-refused")
        initial = self.mesh_launch(session, slot="mac")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        seat = next(iter(cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))))
        environment, _ = self.claim_fixture(session, "blocked")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", seat, "--phase", "3", str(session), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "mac"],
            check=False, capture_output=True, text=True, env=environment, timeout=300,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("phase 3 could not claim its files", result.stderr)
        events = (session / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("follow", events[events.index("claim"):], events)

    def test_an_unreadable_ledger_opens_the_seat_with_the_phase_still_awaiting_first_touch(self) -> None:
        # Ledger loss fails open for edits, so a scratch clone with no ledger still opens its seat.
        session = self.start_phase("claim-unreadable")
        initial = self.mesh_launch(session, slot="mac")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        seat = next(iter(cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))))
        environment, _ = self.claim_fixture(session, "ledger_unreadable")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", seat, "--phase", "3", str(session), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "mac"],
            check=False, capture_output=True, text=True, env=environment, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = (session / "phases" / "3" / "delegated_phase_reservation_state.json").read_text(encoding="utf-8")
        self.assertIn("enrolled_awaiting_first_touch", state)
        events = (session / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("follow", events[events.index("claim"):], events)

    def test_a_dispatch_names_its_director_in_the_run_folder(self) -> None:
        # The production tree reads the director here: its one marker names only one of the runs
        # it has open side by side, and a run opened by hand has none.
        session_dir = self.start_phase("director")
        environment = self.environment()
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        result = subprocess.run(
            ["bash", str(self.implement_script), str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=environment, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "director").read_text(encoding="utf-8"), "sid-director\n")

    def mesh_environment(self, fail: bool = False) -> dict[str, str]:
        environment = self.environment()
        environment["UNIT_CODEX_MESH"] = "1"
        if fail:
            environment["STUB_FOLLOW_FAIL"] = "1"
        return environment

    def test_identity_probes_default_to_thirty_one_second_waits_for_new_and_followed_workers(self) -> None:
        for follow in (False, True):
            with self.subTest(follow=follow):
                session = self.start_phase(f"default-probes-{follow}")
                arguments = ["bash", str(self.implement_script)]
                if follow:
                    initial = self.mesh_launch(session)
                    self.assertEqual(initial.returncode, 0, initial.stderr)
                    roster = cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))
                    arguments.extend(["--to", next(iter(roster))])
                arguments.extend([str(session), str(self.working_dir), str(self.prompt_file),
                                  "impl", "the retry path", "impl", "writing the retry path", "0", "impl"])
                binaries = session / "bin"
                binaries.mkdir()
                sleep = binaries / "sleep"
                _ = sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_PROBES"\n')
                sleep.chmod(0o755)
                watcher = self.root / "scripts/agents/heartbeat_watch.sh"
                _ = watcher.write_text('#!/bin/bash\nif [[ -n "${TEST_PROBES_FINISHED:-}" ]]; then '
                                       + ': > "$TEST_PROBES_FINISHED"; fi\n')
                mesh = self.root / "scripts/agents/codex_mesh.py"
                _ = mesh.write_text(STUB_MESH.replace(
                    "verb = sys.argv[1]", "verb = sys.argv[1]\n"
                    + "if verb in ('start', 'follow'):\n"
                    + "    import subprocess\n"
                    + "    agent_pid = pathlib.Path(os.environ['TEST_AGENT_PID'])\n"
                    + "    agent_pid.with_name(agent_pid.name + '.tmp').write_text(str(os.getpid()))\n"
                    + "    os.replace(agent_pid.with_name(agent_pid.name + '.tmp'), agent_pid)\n"
                    + "    subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], os.environ['TEST_DEFAULT_RELEASE']], check=True)"
                ))
                probes = session / "probes"
                finished = session / "probes-finished"
                release = session / "release"
                pid_file = session / "agent-pid"
                environment = self.mesh_environment()
                for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                            "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
                    _ = environment.pop(key, None)
                environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                                    "TEST_PROBES": str(probes), "TEST_PROBES_FINISHED": str(finished),
                                    "TEST_AGENT_PID": str(pid_file), "TEST_DEFAULT_RELEASE": str(release)})
                process = subprocess.Popen(arguments, env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    wait_until(lambda: finished.exists(), session, processes=(process,))
                    os.kill(wait_for_pid(pid_file, processes=(process,)), 0)
                    self.assertIsNone(process.poll())
                    self.assertEqual(probes.read_text().splitlines(), ["1"] * 30)
                    if follow:
                        claim = cast("dict[str, object]", json.loads((session / "impl_claim_impl.json").read_text()))
                        self.assertEqual(claim["turn_id"], "")
                    else:
                        self.assertFalse((session / "impl_worker_impl.json").exists())
                finally:
                    output, errors = self.finish_default_launcher(process, release, mesh)
                self.assertEqual(process.returncode, 0, output + errors)

    def finish_default_launcher(self, process: subprocess.Popen[str], release: Path,
                                mesh: Path, timeout: float = 300) -> tuple[str, str]:
        try:
            release.touch()
            return communicate_launcher(process, timeout)
        finally:
            # A timeout must also restore the fixture for the next subcase.
            _ = mesh.write_text(STUB_MESH)

    def test_default_launcher_timeout_reaps_its_group_and_restores_the_mesh(self) -> None:
        ready = self.root / "blocked-launcher-child"
        child_code = ("import os, signal, sys; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                      "p = Path(sys.argv[1]); staged = p.with_name(p.name + '.tmp'); "
                      "staged.write_text(str(os.getpid())); os.replace(staged, p); signal.pause()")
        program = f"import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(ready)!r}]); child.wait()"
        process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        mesh = self.root / "scripts/agents/codex_mesh.py"
        _ = mesh.write_text("blocked fixture")
        try:
            descriptor = os.pidfd_open(wait_for_pid(ready, processes=(process,)))
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = self.finish_default_launcher(process, self.root / "release", mesh, timeout=0)
                self.assertIsNotNone(process.poll())
                self.assertEqual(select.select([descriptor], [], [], 300)[0], [descriptor])
                self.assertEqual(mesh.read_text(), STUB_MESH)
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
            env=environment, timeout=300,
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
                check=False, capture_output=True, text=True, env=environment, timeout=300,
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
            check=False, capture_output=True, text=True, env=environment, timeout=300,
        )

    def open_passes(self, session_dir: Path) -> dict[str, OpenPass]:
        stored = cast(
            "ProgressState",
            json.loads((session_dir / "progress_history_state.json").read_text(encoding="utf-8")),
        )
        return stored["pass"]

    def test_new_claude_worker_uses_registry_effort(self) -> None:
        for row, expected_effort in (("sonnet:low", "low"), ("sonnet", "")):
            with self.subTest(row=row):
                session_dir = self.start_phase(f"effort-{row.replace(':', '-')}")
                result = self.claude_launch(
                    session_dir, row, inherited_effort="xhigh"
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = cast("list[str]", json.loads(
                    (self.root / "launch_args.json").read_text(encoding="utf-8")
                ))
                self.assertEqual(arguments[arguments.index("--model") + 1], "sonnet")
                if expected_effort:
                    self.assertEqual(
                        arguments[arguments.index("--effort") + 1], expected_effort
                    )
                else:
                    self.assertNotIn("--effort", arguments)
                self.assertEqual(
                    (session_dir / "impl_bg_id_impl").read_text(encoding="utf-8"),
                    "abc12345\n",
                )
                self.assertIn(
                    "abc12345\t", (session_dir / "workers").read_text(encoding="utf-8")
                )

    def test_copied_claude_launcher_keeps_legacy_home_then_uses_showrunners_after_cutover(self) -> None:
        with patch.dict(os.environ, self.environment(), clear=True):
            self.assertEqual(accounts.setup("claude2"), [])
            _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({
                "oauthAccount": {"emailAddress": "worker@example.invalid"},
            }))
            groups = accounts.groups_file()
            groups.parent.mkdir(parents=True, exist_ok=True)
            _ = groups.write_text(json.dumps({"showrunners": "claude2"}))
        for cutover in (False, True):
            with self.subTest(cutover=cutover):
                if cutover:
                    _ = (self.root / ".claude.json").write_text("{}")
                session_dir = self.start_phase(f"copied-account-{cutover}")

                result = self.claude_launch(session_dir, "sonnet:low")

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((self.root / "launch_environment.json").read_text()), {
                    "CLAUDE_CONFIG_DIR": str(self.root / ".claude2") if cutover else "",
                    "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(self.root / ".claude") if cutover else "",
                })

    def test_claude_pane_fallback_strips_controls_before_character_cut(self) -> None:
        session_dir = self.start_phase("pane-cleanup")
        raw = (
            "é" * 4050 + "\x1b]0;Title\x07\x1b]1;Other\x1b\\"
            + "\x1b[31mVisible\r text\b\x1b[2K\x1b[0m\x1b7\nNext line\n"
        )
        _ = (self.root / "pane.bin").write_bytes(raw.encode("utf-8"))

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = (session_dir / "impl_summary_impl.txt").read_text(encoding="utf-8")
        expected = ("é" * 4050 + "Visible text\nNext line\n")[-4000:]
        self.assertEqual(summary, expected)
        self.assertEqual(len(summary), 4000)
        self.assertNotIn("\x1b", summary)
        self.assertNotIn("\r", summary)
        self.assertEqual((session_dir / "impl_agent_impl.log").read_bytes(), raw.encode("utf-8"))

    def test_claude_pane_fallback_keeps_newline_and_tab_after_a_stray_escape(self) -> None:
        session_dir = self.start_phase("pane-stray-escape")
        _ = (self.root / "pane.bin").write_bytes(
            "Before\x1b\nMiddle\x1b\tTail\x1b(Bend\n".encode("utf-8")
        )

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (session_dir / "impl_summary_impl.txt").read_text(encoding="utf-8"),
            "Before\nMiddle\tTailend\n",
        )

    def test_claude_control_only_pane_uses_no_summary_line(self) -> None:
        session_dir = self.start_phase("pane-controls-only")
        _ = (self.root / "pane.bin").write_bytes(
            b"\x1b[31m\x1b]0;Title\x07\x1b]1;Other\x1b\\\x1b7\r\b\x07"
        )

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = (session_dir / "impl_summary_impl.txt").read_text(encoding="utf-8")
        arguments = cast("list[str]", json.loads(
            (self.root / "launch_args.json").read_text(encoding="utf-8")
        ))
        name = arguments[arguments.index("--name") + 1]
        self.assertEqual(
            summary, f"The background agent {name} produced no summary.\n",
        )

    def test_claude_written_summary_is_preserved_byte_for_byte(self) -> None:
        session_dir = self.start_phase("own-summary")
        original = b"Worker summary\r\n\x1b[31mcolour\x1b[0m\n"
        _ = (self.root / "own_summary.bin").write_bytes(original)
        _ = (self.root / "pane.bin").write_bytes(b"Other pane output\n")

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "impl_summary_impl.txt").read_bytes(), original)

    def test_a_dispatch_with_no_pass_kind_is_refused(self) -> None:
        # What the incident actually looked like on the wire: a worker launched
        # with the sixth argument dropped. It used to run the agent and record
        # nothing; now nothing runs at all.
        session_dir = self.start_phase("dropped")
        result = subprocess.run(
            [
                "bash", str(self.implement_script), str(session_dir), str(self.working_dir),
                str(self.prompt_file), "test", "the retry tests", "",
                "writing the retry tests", "0", "test",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=300,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("pass_kind must be impl, test, fix, or review", result.stderr)
        # Refused before anything ran: no status file to be read as a live worker,
        # and no register line for peers to answer.
        self.assertFalse((session_dir / "impl_status_test").exists())
        self.assertFalse((session_dir / "board.log").exists())

    def test_an_unknown_pass_kind_is_refused_by_the_same_check(self) -> None:
        session_dir = self.start_phase("unknown")
        result = self.launch(session_dir, "implementation", "impl")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("got 'implementation'", result.stderr)

    def test_every_worker_records_its_own_pass_and_stamps_its_role(self) -> None:
        session_dir = self.start_phase("team")
        for kind, slot in (("impl", "impl"), ("impl", "test")):
            result = self.launch(session_dir, kind, slot)
            self.assertEqual(result.returncode, 0, result.stderr)

        # Two records, keyed by worker rather than by what each is doing: the
        # `test` worker opened as a second writer and is still slot `test`.
        passes = self.open_passes(session_dir)
        self.assertEqual(
            {slot: record["kind"] for slot, record in passes.items()},
            {"impl": "impl", "test": "impl"},
        )
        self.assertEqual(
            {record["status"] for record in passes.values()}, {"completed"}
        )

        # The register line carries the role, which is what fills the progress
        # table's columns before any agent has posted, and what made the
        # incident legible from the board alone once it had gone wrong.
        register = [
            line
            for line in (session_dir / "board.log").read_text(encoding="utf-8").splitlines()
            if "register:" in line
        ]
        self.assertEqual(len(register), 2, register)
        for line, expected in zip(register, ("role=impl", "role=impl")):
            self.assertIn(expected, line)

    def test_follow_up_records_pass_and_lands_its_repair_round(self) -> None:
        session_dir = self.start_phase("landed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.open_repair_round(session_dir)

        result = self.mesh_launch(
            session_dir, to=name, kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repair_outcome(session_dir), "landed")
        self.assertEqual(self.open_passes(session_dir)["impl"]["kind"], "fix")
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")
        self.assertEqual(self.open_passes(session_dir)["impl"]["worker_name"], name)
        # A renamed unit's seat works in the worktree the launcher names, not the one its thread began in.
        self.assertEqual((session_dir / "follow_cwd.txt").read_text(encoding="utf-8"), str(self.working_dir))
        message = (session_dir / "follow_message.txt").read_text(encoding="utf-8")
        self.assertIn("Write the retry path.", message)
        self.assertIn("impl_summary_impl.txt", message)
        self.assertIn("post", message)
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"follow-up to {name}", board)
        self.assertIn("launcher:", board)
        self.assertIn("done:", board)

    def test_follow_refuses_transferring_and_redirected_roles_before_compaction_or_claim(self) -> None:
        session = self.start_phase("adoption-fence")
        initial = self.mesh_launch(session)
        self.assertEqual(initial.returncode, 0, initial.stderr)
        path = session / "mesh_roster.json"
        original = cast(dict[str, dict[str, object]], json.loads(path.read_text()))
        name = next(iter(original))
        self.open_repair_round(session)
        for field, value in (("transferring_role", {"adoption_id": "transfer-one", "destination": "/fixture/roles/mesh"}),
                             ("role_redirect", {"design_id": "lasting-design", "destination": "/fixture/roles/mesh"})):
            with self.subTest(field=field):
                roster = {key: dict(entry) for key, entry in original.items()}
                roster[name].update({field: value, "context_tokens": 999999})
                _ = path.write_text(json.dumps(roster))
                before = path.read_bytes()
                events = session / "mesh_events.txt"
                previous_events = events.read_bytes() if events.exists() else b""
                result = self.mesh_launch(session, to=name, kind="fix", resolves_round=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("TransferringRole", result.stderr)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(events.read_bytes() if events.exists() else b"", previous_events)

    def test_follow_up_compacts_above_the_context_threshold_before_claiming(self) -> None:
        session_dir = self.start_phase("compact-above")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 0, result.stderr)
        events = (session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(events, ["compact", "can-follow", "follow"])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(
            f"launcher: compacted {name}: 100001 -> 12000 tokens",
            board,
        )

    def test_follow_up_to_an_unmeasured_worker_does_not_compact_again(self) -> None:
        session_dir = self.start_phase("unmeasured-follow")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        path = session_dir / "mesh_roster.json"
        roster = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        name = next(iter(roster))
        roster[name]["context_tokens"] = "not_measured_since_compaction"
        _ = path.write_text(json.dumps(roster), encoding="utf-8")

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["can-follow", "follow"])
        stored = cast(dict[str, dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(stored[name]["context_tokens"], "not_measured_since_compaction")

    def test_follow_up_posts_the_unmeasured_compaction_line_unchanged(self) -> None:
        session_dir = self.start_phase("compact-unmeasured-line")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast(dict[str, object], json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)
        (session_dir / "compact_reports_zero").touch()

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text(encoding="utf-8").splitlines(),
                         ["compact", "can-follow", "follow"])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"launcher: compacted {name}: 100001 tokens, size not measured until its next turn", board)

    def test_start_and_follow_write_the_current_kind_as_the_roster_role(self) -> None:
        session_dir = self.start_phase("roster-role")
        first = self.mesh_launch(session_dir, kind="impl")
        self.assertEqual(first.returncode, 0, first.stderr)
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.assertEqual(roster[name]["role"], "impl")
        self.assertEqual(roster[name]["task"], "direct.impl")

        followed = self.mesh_launch(
            session_dir, to=name, kind="review", subtask="impl"
        )

        self.assertEqual(followed.returncode, 0, followed.stderr)
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        self.assertEqual(roster[name]["role"], "review")
        self.assertEqual(roster[name]["task"], "direct.impl")

    def test_an_impl_worker_keeps_one_identity_when_followed_as_a_repair_worker(self) -> None:
        run = self.start_phase("impl-becomes-fix")
        opened = self.mesh_launch(run, kind="impl")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        path = run / "impl_worker_impl.json"
        original = cast(dict[str, object], json.loads(path.read_text()))
        name = str(original["name"])

        followed = self.mesh_launch(run, to=name, kind="fix", fix_cause="repair identity after implementation")

        self.assertEqual(followed.returncode, 0, followed.stderr)
        records = [cast(dict[str, object], json.loads(item.read_text()))
                   for item in run.glob("impl_worker_*.json")]
        identities = [record for record in records if record.get("name") == name]
        self.assertEqual(len(identities), 1, records)
        self.assertEqual(identities[0]["worker_id"], original["worker_id"])
        self.assertEqual(cast(dict[str, object], json.loads(path.read_text()))["name"], name)
        roster = cast(dict[str, dict[str, object]], json.loads((run / "mesh_roster.json").read_text()))
        self.assertEqual(roster[name]["role"], "fix")
        self.assertFalse((run / "impl_worker_fix.json").exists())

    def test_compaction_threshold_must_be_configured_as_a_positive_integer(self) -> None:
        configured = self.config_file.read_text(encoding="utf-8")
        key = "UNIT_COMPACT_ABOVE_TOKENS"
        without_key = "\n".join(
            line for line in configured.splitlines() if not line.startswith(f"{key}=")
        ) + "\n"
        for label, config in (("missing", without_key), ("zero", without_key + f"{key}=0\n")):
            with self.subTest(value=label):
                _ = self.config_file.write_text(config, encoding="utf-8")
                session_dir = self.start_phase(f"threshold-{label}")

                result = self.mesh_launch(session_dir)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(key, result.stderr)

    def test_follow_up_does_not_compact_at_or_below_the_context_threshold(self) -> None:
        for tokens in (99999, 100000):
            with self.subTest(tokens=tokens):
                session_dir = self.start_phase(f"compact-below-{tokens}")
                first = self.mesh_launch(session_dir)
                self.assertEqual(first.returncode, 0, first.stderr)
                roster = cast("dict[str, object]", json.loads(
                    (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
                ))
                name = next(iter(roster))
                self.set_mesh_context(session_dir, name, tokens)

                result = self.mesh_launch(session_dir, to=name)

                self.assertEqual(result.returncode, 0, result.stderr)
                events = (session_dir / "mesh_events.txt").read_text(
                    encoding="utf-8"
                ).splitlines()
                self.assertEqual(events, ["can-follow", "follow"])

    def test_failed_compaction_is_reported_and_the_follow_up_still_runs(self) -> None:
        session_dir = self.start_phase("compact-fails")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)

        result = self.mesh_launch(session_dir, to=name, compact_fail=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session_dir / "follow_message.txt").exists())
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("compaction failed", board)
        self.assertIn("continuing follow-up", board)

    def test_a_worker_left_with_a_live_turn_by_compaction_gets_no_follow_up(self) -> None:
        session_dir = self.start_phase("compact-still-live")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 100001)

        result = self.mesh_launch(session_dir, to=name, compact_still_live=True)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("still has a live turn after compaction; end the worker", result.stderr)
        self.assertFalse((session_dir / "follow_message.txt").exists())
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)
        self.assertNotIn("continuing follow-up", board)

    def test_success_status_follows_pass_and_landed_records(self) -> None:
        session_dir = self.start_phase("status-after-landed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.open_repair_round(session_dir)
        self.observe_status_at_records()

        result = self.mesh_launch(
            session_dir, to=next(iter(roster)), kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "landed"), "implementing\n")
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"),
            "implemented\n",
        )

    def test_failed_landed_record_leaves_error_status(self) -> None:
        session_dir = self.start_phase("status-landed-fails")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.open_repair_round(session_dir)
        self.observe_status_at_records(fail_landed=True)

        result = self.mesh_launch(
            session_dir, to=next(iter(roster)), kind="fix", resolves_round=True
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "landed"), "implementing\n")
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"), "error\n"
        )
        self.assertIn("blocked:", (session_dir / "board.log").read_text(encoding="utf-8"))

    def test_worker_error_status_follows_pass_and_abandon_records(self) -> None:
        session_dir = self.start_phase("status-after-abandon")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.open_repair_round(session_dir)
        self.observe_status_at_records()

        result = self.mesh_launch(
            session_dir, to=next(iter(roster)), kind="fix", fail=True,
            resolves_round=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.observed_status(session_dir, "finish-pass"), "implementing\n")
        self.assertEqual(self.observed_status(session_dir, "abandon"), "implementing\n")
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"), "error\n"
        )

    def test_failed_follow_up_abandons_round_and_posts_blocked(self) -> None:
        session_dir = self.start_phase("abandoned")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.open_repair_round(session_dir)

        result = self.mesh_launch(
            session_dir, to=name, kind="fix", fail=True, resolves_round=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.repair_outcome(session_dir), "abandoned", result.stderr)
        state = cast("dict[str, object]", json.loads(
            (session_dir / "findings_state.json").read_text(encoding="utf-8")
        ))
        rounds = cast("list[dict[str, object]]", state["rounds"])
        self.assertIs(rounds[-1]["edits_landed"], True)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("blocked:", board)

    def test_claude_repair_in_the_impl_slot_keeps_a_distinct_worker_identity(self) -> None:
        run = self.start_phase("claude-repair-identity")
        first = self.claude_launch(run, "sonnet:low")
        self.assertEqual(first.returncode, 0, first.stderr)
        original = (run / "impl_worker_impl.json").read_bytes()
        self.open_repair_round(run)
        repair = self.claude_launch(run, "sonnet:low", kind="fix")
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertEqual((run / "impl_worker_impl.json").read_bytes(), original)
        identity = cast(dict[str, object], json.loads((run / "impl_worker_fix.json").read_text()))
        original_identity = cast(dict[str, object], json.loads(original))
        self.assertNotEqual(identity["name"], original_identity["name"])
        self.assertEqual(identity["family"], "claude")
        self.assertFalse((run / "mesh_roster.json").exists())

    def test_a_repair_pass_leaves_the_impl_worker_followable(self) -> None:
        session_dir = self.start_phase("repair-keeps-impl")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        impl = cast("dict[str, object]", json.loads((session_dir / "impl_worker_impl.json").read_text()))
        repair = self.mesh_launch(session_dir, kind="fix", fix_cause="retry test failed after integration")
        self.assertEqual(repair.returncode, 0, repair.stderr)
        self.assertEqual(json.loads((session_dir / "impl_worker_impl.json").read_text()), impl)
        fix = cast("dict[str, object]", json.loads((session_dir / "impl_worker_fix.json").read_text()))
        self.assertTrue(str(fix["name"]).endswith("-fix"))
        result = self.mesh_launch(session_dir, to=str(impl["name"]))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_follow_up_reads_an_old_identity_and_leaves_only_worker_names(self) -> None:
        session_dir = self.start_phase("old-run-follow")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        path = session_dir / "impl_worker_impl.json"
        identity = cast("dict[str, object]", json.loads(path.read_text()))
        identity["seat_id"] = identity.pop("worker_id")
        _ = (session_dir / "impl_seat_impl.json").write_text(json.dumps(identity))
        path.unlink()
        result = self.mesh_launch(session_dir, to=str(identity["name"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((session_dir / "impl_seat_impl.json").exists())
        converted = cast("dict[str, object]", json.loads(path.read_text()))
        self.assertEqual(converted["worker_id"], identity["seat_id"])
        self.assertNotIn("seat_id", converted)

    def test_follow_up_of_a_worker_with_no_identity_record_resumes_its_roster_thread(self) -> None:
        session_dir = self.start_phase("lost-identity")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        path = session_dir / "impl_worker_impl.json"
        identity = cast("dict[str, object]", json.loads(path.read_text()))
        path.unlink()
        resumed = self.mesh_launch(session_dir, to=str(identity["name"]))
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(path.read_text()), identity)
        _ = path.write_text(json.dumps({**identity, "worker_id": "another-thread"}))
        refused = self.mesh_launch(session_dir, to=str(identity["name"]))
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("has no matching durable worker identity for slot impl", refused.stderr)

    def test_peer_turn_after_claim_closes_follow_up_as_error(self) -> None:
        session_dir = self.start_phase("peer-after-claim")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        environment = self.mesh_environment()
        environment["STUB_FOLLOW_PEER_AFTER_CLAIM"] = "1"

        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", name,
             str(session_dir), str(self.working_dir), str(self.prompt_file),
             "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            env=environment, capture_output=True, text=True, check=False, timeout=300,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((session_dir / "impl_status_impl").read_text(encoding="utf-8"), "error\n")
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")
        peer = cast("dict[str, dict[str, object]]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.assertEqual(peer[name]["status"], "running")

    def test_unknown_and_busy_follow_ups_leave_pass_and_status_untouched(self) -> None:
        session_dir = self.start_phase("refused")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        status_file = session_dir / "impl_status_impl"
        _ = status_file.write_text("previous\n", encoding="utf-8")
        before = self.open_passes(session_dir)
        unknown = self.mesh_launch(session_dir, to="missing-worker")
        self.assertEqual(unknown.returncode, 2, unknown.stderr)
        self.assertEqual(status_file.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(self.open_passes(session_dir), before)

        roster[name]["status"] = "running"
        _ = roster_path.write_text(json.dumps(roster), encoding="utf-8")
        busy = self.mesh_launch(session_dir, to=name)
        self.assertEqual(busy.returncode, 2, busy.stderr)
        self.assertEqual(status_file.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(self.open_passes(session_dir), before)

    def test_follow_up_rejects_wrong_slot_and_keeps_its_recorded_model(self) -> None:
        session_dir = self.start_phase("identity")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        before = self.open_passes(session_dir)

        wrong_slot = self.mesh_launch(session_dir, to=name, slot="test")
        self.assertEqual(wrong_slot.returncode, 2, wrong_slot.stderr)
        self.assertFalse((session_dir / "impl_status_test").exists())
        self.assertEqual(self.open_passes(session_dir), before)

        registry = self.root / ".claude" / "config" / "agents.conf"
        _ = registry.write_text(
            AGENTS_REGISTRY.replace("impl=gpt-called:xhigh", "impl=gpt-blind:max"),
            encoding="utf-8",
        )
        # A registry row changed after the worker opened: the follow-up keeps the worker's model.
        changed_model = self.mesh_launch(session_dir, to=name)
        self.assertEqual(changed_model.returncode, 0, changed_model.stderr)
        self.assertIn("agent=gpt-called\n", (session_dir / "impl_agent_impl").read_text(encoding="utf-8"))
        identity = cast("dict[str, object]", json.loads((session_dir / "impl_worker_impl.json").read_text()))
        self.assertEqual(identity["model"], "gpt-called")

    def test_follow_up_resumes_a_worker_recorded_before_the_rename(self) -> None:
        # Seats recorded before the delegate -> direct rename stored the model of the user's
        # rows at that time, which the registry no longer names (rca of 2026-10-23).
        session_dir = self.start_phase("before-rename")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        path = session_dir / "impl_worker_impl.json"
        identity = cast("dict[str, object]", json.loads(path.read_text()))
        _ = path.write_text(json.dumps({**identity, "model": "gpt-blind"}))
        resumed = self.mesh_launch(session_dir, to=str(identity["name"]))
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("agent=gpt-blind\n", (session_dir / "impl_agent_impl").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(path.read_text()), {**identity, "model": "gpt-blind"})

    def test_follow_recovers_missing_claim_unless_the_roster_launcher_is_alive(self) -> None:
        for launcher_pid in (None, 99999999, os.getpid()):
            live = launcher_pid == os.getpid()
            with self.subTest(launcher_pid=launcher_pid):
                session_dir = self.start_phase(f"missing-claim-{launcher_pid}")
                first = self.mesh_launch(session_dir)
                self.assertEqual(first.returncode, 0, first.stderr)
                roster_path = session_dir / "mesh_roster.json"
                roster = cast(dict[str, dict[str, object]], json.loads(roster_path.read_text()))
                name = next(iter(roster))
                roster[name].update(status="paused", paused_from="running", paused_turn=True)
                if launcher_pid is not None:
                    roster[name]["launcher_pid"] = launcher_pid
                _ = roster_path.write_text(json.dumps(roster))
                status = session_dir / "impl_status_impl"
                _ = status.write_text("implementing\n")
                self.assertFalse((session_dir / "impl_claim_impl.json").exists())
                before = self.open_passes(session_dir)

                result = self.mesh_launch(session_dir, to=name)

                if live:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(status.read_text(), "implementing\n")
                    self.assertEqual(self.open_passes(session_dir), before)
                    self.assertFalse((session_dir / "follow_message.txt").exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((session_dir / "status_before_claim.txt").read_text(), "error\n")
                    self.assertEqual(status.read_text(), "implemented\n")
                    followed = cast(dict[str, dict[str, object]], json.loads(roster_path.read_text()))
                    self.assertEqual(followed[name]["thread_id"], roster[name]["thread_id"])
                    self.assertTrue((session_dir / "follow_message.txt").exists())

    def test_live_claim_refuses_a_second_launcher_without_a_pass(self) -> None:
        session_dir = self.start_phase("claimed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        _ = (session_dir / "impl_claim_impl.json").write_text(json.dumps({
            "pid": os.getpid(), "worker": name, "turn_id": "",
        }), encoding="utf-8")
        before = self.open_passes(session_dir)

        second = self.mesh_launch(session_dir, to=name)

        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertEqual(self.open_passes(session_dir), before)
        self.assertEqual(
            (session_dir / "impl_claim_impl.json").read_text(encoding="utf-8"),
            json.dumps({"pid": os.getpid(), "worker": name, "turn_id": ""}),
        )

    def test_failed_pass_write_posts_blocked_and_does_not_send(self) -> None:
        session_dir = self.start_phase("ledger-error")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        _ = (session_dir / "progress_history_state.json").write_text(
            "{}\n", encoding="utf-8"
        )

        result = self.mesh_launch(session_dir, to=name)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((session_dir / "follow_message.txt").exists())
        self.assertEqual(
            (session_dir / "impl_status_impl").read_text(encoding="utf-8"),
            "error\n",
        )
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("blocked:", board)

    def test_killed_launcher_claim_recovers_before_and_after_turn_start(self) -> None:
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                session_dir = self.start_phase(f"claim-{stage}")
                first = self.mesh_launch(session_dir)
                self.assertEqual(first.returncode, 0, first.stderr)
                roster_path = session_dir / "mesh_roster.json"
                roster = cast("dict[str, dict[str, object]]", json.loads(
                    roster_path.read_text(encoding="utf-8")
                ))
                name = next(iter(roster))
                environment = self.mesh_environment()
                environment["STUB_FOLLOW_HOLD"] = stage
                process = subprocess.Popen(
                    ["bash", str(self.implement_script), "--to", name,
                     str(session_dir), str(self.working_dir), str(self.prompt_file),
                     "impl", "the retry path", "impl", "writing the retry path",
                     "0", "impl"],
                    env=environment, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
                claim_path = session_dir / "impl_claim_impl.json"
                try:
                    def held_claim() -> bool:
                        try:
                            claim = cast("dict[str, object]", json.loads(
                                claim_path.read_text(encoding="utf-8")
                            ))
                            roster = cast("dict[str, dict[str, object]]", json.loads(
                                roster_path.read_text(encoding="utf-8")
                            ))
                        except (FileNotFoundError, json.JSONDecodeError):
                            return False
                        return (claim.get("turn_id") == "" if stage == "before"
                                else roster[name]["status"] == "running")

                    wait_until(held_claim, session_dir)
                finally:
                    os.killpg(process.pid, signal.SIGKILL)
                    _ = process.wait(timeout=300)

                result = self.mesh_launch(session_dir, to=name)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    (session_dir / "impl_status_impl").read_text(encoding="utf-8"),
                    "implemented\n",
                )
                self.assertFalse((session_dir / "impl_claim_impl.json").exists())

    def test_mesh_disabled_follow_refuses_before_recording_a_pass(self) -> None:
        session_dir = self.start_phase("mesh-disabled")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        status_path = session_dir / "impl_status_impl"
        before = status_path.read_text(encoding="utf-8")
        passes = self.open_passes(session_dir)
        args = ["bash", str(self.implement_script), "--to", name,
                str(session_dir), str(self.working_dir), str(self.prompt_file),
                "impl", "the retry path", "impl", "writing the retry path", "0", "impl"]

        refused = subprocess.run(args, env=self.environment(), capture_output=True,
                                 text=True, check=False, timeout=300)

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(status_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.open_passes(session_dir), passes)
        self.assertNotIn("follow-up to", (session_dir / "board.log").read_text())

    def test_worker_and_turn_identity_are_recorded_before_the_heartbeat_watcher_starts(self) -> None:
        for follow in (False, True):
            with self.subTest(follow=follow):
                session = self.start_phase(f"identity-before-heartbeat-{follow}")
                arguments = ["bash", str(self.implement_script)]
                if follow:
                    initial = self.mesh_launch(session)
                    self.assertEqual(initial.returncode, 0, initial.stderr)
                    roster = cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))
                    arguments.extend(["--to", next(iter(roster))])
                arguments.extend([str(session), str(self.working_dir), str(self.prompt_file),
                                  "impl", "the retry path", "impl", "writing the retry path", "0", "impl"])
                witness = session / "watcher-identity"
                source = session / ("impl_claim_impl.json" if follow else "impl_worker_impl.json")
                watcher = self.root / "scripts" / "agents" / "heartbeat_watch.sh"
                _ = watcher.write_text(
                    '#!/bin/bash\nif [ -f "$TEST_IDENTITY_SOURCE" ]; then\n'
                    + 'cat "$TEST_IDENTITY_SOURCE" > "$TEST_IDENTITY_WITNESS.tmp"\n'
                    + 'else printf "{}\\n" > "$TEST_IDENTITY_WITNESS.tmp"; fi\n'
                    + 'mv "$TEST_IDENTITY_WITNESS.tmp" "$TEST_IDENTITY_WITNESS"\n'
                )
                release = session / "launch-release"
                environment = self.mesh_environment()
                environment.update({"TEST_IDENTITY_SOURCE": str(source), "TEST_IDENTITY_WITNESS": str(witness),
                                    "STUB_LAUNCH_RELEASE": str(release)})
                if follow:
                    environment.update({"STUB_FOLLOW_HOLD": "after", "STUB_LAUNCH_RELEASE": str(release)})
                process = subprocess.Popen(arguments, env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    wait_for_file(witness)
                    identity = cast("dict[str, object]", json.loads(witness.read_text()))
                    self.assertIsNone(process.poll())
                    if follow:
                        self.assertEqual(identity.get("turn_id"), "turn-2")
                        self.assertEqual(identity.get("pid"), process.pid)
                    else:
                        self.assertTrue(identity.get("worker_id"), identity)
                finally:
                    release.touch()
                    output, errors = process.communicate(timeout=300)
                self.assertEqual(process.returncode, 0, output + errors)

    def test_new_launch_keeps_heartbeat_watcher_output(self) -> None:
        watcher = self.root / "scripts" / "agents" / "heartbeat_watch.sh"
        _ = watcher.write_text(
            "#!/usr/bin/env bash\nprintf 'heartbeat watcher output\\n'\n: > \"$TEST_HEARTBEAT_READY\"\n",
            encoding="utf-8",
        )
        agent = self.root / "scripts" / "agents" / "agent_exec.sh"
        _ = agent.write_text(
            agent.read_text(encoding="utf-8").replace(
                "set -euo pipefail", 'set -euo pipefail\npython3 "$TEST_WAIT_HELPER" "$TEST_HEARTBEAT_READY"'
            ), encoding="utf-8",
        )
        session_dir = self.start_phase("heartbeat-visible")

        result = self.launch(session_dir, "impl", "impl")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("heartbeat watcher output", result.stdout)

    def test_failed_worker_with_live_turn_refuses_before_recording_a_pass(self) -> None:
        session_dir = self.start_phase("failed-live-turn")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(roster_path.read_text()))
        name = next(iter(roster))
        roster[name]["status"] = "failed"
        roster[name]["live_turn"] = True
        _ = roster_path.write_text(json.dumps(roster))
        status_path = session_dir / "impl_status_impl"
        before = status_path.read_text(encoding="utf-8")
        passes = self.open_passes(session_dir)

        refused = self.mesh_launch(session_dir, to=name)

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(status_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.open_passes(session_dir), passes)

    def test_claude_follow_counts_turn_completed_before_first_poll(self) -> None:
        session_dir = self.start_phase("claude-short-turn")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "complete")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")
        self.assertIn("impl_summary_impl.txt", (
            self.root / "delivered_message.txt"
        ).read_text(encoding="utf-8"))

    def test_claude_queued_send_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-queued")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "queued")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")
        self.assertIn("blocked:", (
            session_dir / "board.log"
        ).read_text(encoding="utf-8"))

    def test_claude_worker_gone_after_delivery_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-gone")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "gone")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")

    def test_claude_follow_retries_unreadable_agents_listing(self) -> None:
        session_dir = self.start_phase("claude-unreadable")
        transcript = self.claude_worker(session_dir)

        result = self.claude_follow(session_dir, transcript, "unreadable_once")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")

if __name__ == "__main__":
    _ = unittest.main()
