#!/usr/bin/env python3
"""Integration tests for implement.sh's seat arguments.

The failure these exist for left no error anywhere. A repair round went out with
the pass kind on one seat, the other two launchers ran their agents normally,
and the ledger kept describing the round before it -- two records closed `error`
an hour earlier -- until the one live window closed and the recorder began
refusing every progress call while two agents were still working.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import TypedDict, cast, override


DELEGATE_DIR = Path(__file__).parent
AGENTS_DIR = DELEGATE_DIR.parent / "agents"

# The wrapper calls this as: <task> write <working_dir> <prompt> <reply> <log>.
# Writing both outputs and exiting immediately drives the wrapper's completion
# branch without a model call; the wrapper copies the reply into the summary
# file because the stub, like a delegate that skipped its last act, left it
# empty.
STUB_AGENT_EXEC = """#!/usr/bin/env bash
set -euo pipefail
printf 'Wrote the retry path.\\n' > "$5"
printf 'stub agent log\\n' > "$6"
"""

STUB_MESH = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

verb = sys.argv[1]
def option(name):
    return sys.argv[sys.argv.index(name) + 1]

session = pathlib.Path(option('--session-dir'))
roster_path = session / 'mesh_roster.json'
roster = json.loads(roster_path.read_text()) if roster_path.exists() else {}
if verb in ('compact', 'can-follow', 'follow'):
    with (session / 'mesh_events.txt').open('a') as events:
        events.write(verb + '\\n')
if verb == 'start':
    name = option('--name')
    roster[name] = {'thread_id': 'thread-for-' + name, 'turn_id': 'turn-1',
                    'status': 'done', 'role': option('--role')}
    pathlib.Path(option('--reply-file')).write_text('initial reply\\n')
elif verb == 'compact':
    name = option('--to')
    if os.environ.get('STUB_COMPACT_FAIL') == '1':
        print('stub compaction failed', file=sys.stderr)
        sys.exit(1)
    before = roster[name].get('context_tokens', 0)
    after = int(os.environ.get('STUB_COMPACT_AFTER', '12000'))
    roster[name]['context_tokens'] = after
    roster_path.write_text(json.dumps(roster))
    print(f'compacted {name}: {before} -> {after} tokens')
elif verb == 'can-follow':
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
    if status not in ('done', 'failed') and not (
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
        time.sleep(60)
    roster[name]['role'] = option('--role')
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
        time.sleep(60)
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
"""

CLAUDE_REGISTRY = """[assignments]
delegate=claude

[delegate.claude]
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
    (root / 'seat_state').write_text('busy')
    own_summary = root / 'own_summary.bin'
    if own_summary.exists():
        pathlib.Path(os.environ['STUB_SUMMARY']).write_bytes(own_summary.read_bytes())
    name = sys.argv[sys.argv.index('--name') + 1]
    print(f'backgrounded · abc12345 · {name}')
elif sys.argv[1:3] == ['agents', '--json']:
    unreadable = root / 'unreadable_listing_once'
    if unreadable.exists():
        unreadable.unlink()
        print('not JSON')
        sys.exit(0)
    status = (root / 'seat_state').read_text().strip()
    if status == 'busy':
        (root / 'seat_state').write_text('idle')
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
    (root / 'seat_state').write_text('gone\\n')
if behavior == 'unreadable_once':
    (root / 'unreadable_listing_once').write_text('yes\\n')
print('DELIVERED')
"""


class OpenPass(TypedDict):
    kind: str
    status: str


# "pass" is a keyword, so the recorder's state key needs the functional form.
ProgressState = TypedDict("ProgressState", {"pass": dict[str, OpenPass]})


AGENTS_REGISTRY = """[assignments]
delegate=codex

[delegate.codex]
impl=gpt-called:xhigh
test=gpt-called:xhigh
fix=gpt-called:xhigh
review=gpt-blind:max

[codex.agents]
gpt-called=low,medium,high,xhigh,max
gpt-blind=low,medium,high,xhigh,max
"""


class ImplementLauncherSeatTests(unittest.TestCase):
    """Every seat carries a kind, and the board line says which."""

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
            ["git", "init", "-b", "feature/seats"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        self.config_file = self.root / "delegate.conf"
        _ = self.config_file.write_text(
            "\n".join((
                "PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS=180",
                "PLAN_DELEGATE_COMPACT_ABOVE_TOKENS=100000",
                "MAX_FIX_ATTEMPTS=3", "MAX_REOPENS=3", "STALLED_ROUNDS=3",
                "RUNAWAY_ROUNDS=10", "REPAIR_ROUNDS_PER_FINDING=2",
                "MIN_REPAIR_BUDGET=2", "MAX_CONSECUTIVE_SAME_KIND_PASSES=6",
                "MAX_REVIEW_CANCELLATIONS=1", "",
            )),
            encoding="utf-8",
        )
        registry = self.root / ".claude" / "config" / "agents.conf"
        registry.parent.mkdir(parents=True)
        _ = registry.write_text(AGENTS_REGISTRY, encoding="utf-8")
        self.prompt_file = self.root / "implementation_prompt.md"
        _ = self.prompt_file.write_text("Write the retry path.\n", encoding="utf-8")
        self.implement_script = self.build_script_tree()

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build_script_tree(self) -> Path:
        """Copy the real wrapper beside a stub agent, so nothing calls a model."""
        delegate = self.root / "scripts" / "delegate"
        agents = self.root / "scripts" / "agents"
        lib = self.root / "scripts" / "lib"
        delegate.mkdir(parents=True)
        agents.mkdir(parents=True)
        lib.mkdir(parents=True)
        # The wrapper reaches python3 through ../lib/py, resolved from its own
        # location, so the copy needs the interpreter shim beside it too.
        _ = shutil.copy2(DELEGATE_DIR.parent / "lib" / "py", lib / "py")
        home_lib = self.root / ".claude" / "scripts" / "lib"
        home_lib.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(DELEGATE_DIR.parent / "lib" / "py", home_lib / "py")
        for name in ("implement.sh", "seat_name.sh", "progress_history.py", "findings.py", "board.sh"):
            _ = shutil.copy2(DELEGATE_DIR / name, delegate / name)
        launcher = delegate / "implement.sh"
        _ = launcher.write_text(
            launcher.read_text(encoding="utf-8").replace(
                "HEARTBEAT_INTERVAL_SECS=60", "HEARTBEAT_INTERVAL_SECS=1"
            ), encoding="utf-8",
        )
        for name in ("agents_config.sh", "heartbeat.sh", "heartbeat_watch.sh"):
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
        return delegate / "implement.sh"

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.root)
        environment["PLAN_DELEGATE_HISTORY_DIR"] = str(self.history_dir)
        environment["PLAN_DELEGATE_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.root / ".claude" / "config" / "agents.conf")
        # The plain launcher, so the stub above stands in for the agent. The mesh
        # path would need a live app-server, which is a different test's subject.
        environment["PLAN_DELEGATE_CODEX_MESH"] = "0"
        environment["TZ"] = "UTC"
        _ = environment.pop("CODEX_THREAD_ID", None)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
        return environment

    def recorder(self, *arguments: str) -> None:
        environment = self.environment()
        environment["PLAN_DELEGATE_PASS_OWNER"] = "launcher"
        result = subprocess.run(
            ["python3", str(DELEGATE_DIR / "progress_history.py"), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
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
            timeout=120,
        )

    def mesh_environment(self, fail: bool = False) -> dict[str, str]:
        environment = self.environment()
        environment["PLAN_DELEGATE_CODEX_MESH"] = "1"
        if fail:
            environment["STUB_FOLLOW_FAIL"] = "1"
        return environment

    def mesh_launch(
        self, session_dir: Path, *, to: str = "", slot: str = "impl",
        kind: str = "impl", fail: bool = False, resolves_round: bool = False,
        compact_fail: bool = False, subtask: str = "",
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            "bash", str(self.implement_script),
            str(session_dir), str(self.working_dir), str(self.prompt_file),
            subtask or kind, "the retry path", kind, "writing the retry path", "0", slot,
        ]
        if to:
            arguments.insert(2, "--to")
            arguments.insert(3, to)
        environment = self.mesh_environment(fail=fail)
        if compact_fail:
            environment["STUB_COMPACT_FAIL"] = "1"
        if resolves_round:
            environment["PLAN_DELEGATE_RESOLVES_ROUND"] = "1"
        return subprocess.run(
            arguments, check=False, capture_output=True, text=True,
            env=environment, timeout=120,
        )

    def set_mesh_context(self, session_dir: Path, name: str, tokens: int) -> None:
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        roster[name]["context_tokens"] = tokens
        _ = roster_path.write_text(json.dumps(roster), encoding="utf-8")

    def open_repair_round(self, session_dir: Path) -> None:
        findings = self.root / "scripts" / "delegate" / "findings.py"
        for arguments in (
            ("open", "--severity", "blocker", "--title", "Repair retry",
             "--caught-by", "delegate"),
            ("dispatch", "--covers", "F001"),
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
        delegate = self.root / "scripts" / "delegate"
        for name, commands in (
            ("progress_history.py", ("finish-pass",)),
            ("findings.py", ("landed", "abandon")),
        ):
            script = delegate / name
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

    def claude_seat(self, session_dir: Path) -> Path:
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
        _ = (session_dir / "seats").write_text(
            "abc12345\tfixture-impl\n", encoding="utf-8"
        )
        _ = (session_dir / "impl_bg_id_impl").write_text(
            "abc12345\n", encoding="utf-8"
        )
        _ = (session_dir / "impl_seat_impl.json").write_text(json.dumps({
            "name": "fixture-impl", "slot": "impl", "family": "claude",
            "model": "opus", "seat_id": session_id,
        }), encoding="utf-8")
        _ = (session_dir / "impl_status_impl").write_text(
            "implemented\n", encoding="utf-8"
        )
        _ = (self.root / "seat_state").write_text("idle\n", encoding="utf-8")
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
                check=False, capture_output=True, text=True, env=environment, timeout=30,
            )
        except subprocess.TimeoutExpired as error:
            delivered = self.root / "delivered_message.txt"
            raise AssertionError(
                " ".join((
                    f"Claude follow timed out: transcript={transcript.read_text()!r},",
                    f"delivered={delivered.exists()},",
                    f"seat_state={(self.root / 'seat_state').read_text()!r},",
                    f"stdout={error.stdout!r}, stderr={error.stderr!r}",
                ))
            ) from error

    def claude_launch(
        self, session_dir: Path, registry_row: str, *, inherited_effort: str = ""
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
        environment["STUB_SUMMARY"] = str(session_dir / "impl_summary_impl.txt")
        if inherited_effort:
            environment["AGENT_BG_EFFORT"] = inherited_effort
        else:
            _ = environment.pop("AGENT_BG_EFFORT", None)
        return subprocess.run(
            ["bash", str(self.implement_script), str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl",
             "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=environment, timeout=30,
        )

    def open_passes(self, session_dir: Path) -> dict[str, OpenPass]:
        stored = cast(
            "ProgressState",
            json.loads((session_dir / "progress_history_state.json").read_text(encoding="utf-8")),
        )
        return stored["pass"]

    def test_new_claude_seat_uses_registry_effort(self) -> None:
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
                    "abc12345\t", (session_dir / "seats").read_text(encoding="utf-8")
                )

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
        original = b"Seat summary\r\n\x1b[31mcolour\x1b[0m\n"
        _ = (self.root / "own_summary.bin").write_bytes(original)
        _ = (self.root / "pane.bin").write_bytes(b"Other pane output\n")

        result = self.claude_launch(session_dir, "sonnet:low")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "impl_summary_impl.txt").read_bytes(), original)

    def test_a_dispatch_with_no_pass_kind_is_refused(self) -> None:
        # What the incident actually looked like on the wire: a seat launched
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
            timeout=120,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("pass_kind must be impl, test, fix, or review", result.stderr)
        # Refused before anything ran: no status file to be read as a live seat,
        # and no register line for peers to answer.
        self.assertFalse((session_dir / "impl_status_test").exists())
        self.assertFalse((session_dir / "board.log").exists())

    def test_an_unknown_pass_kind_is_refused_by_the_same_check(self) -> None:
        session_dir = self.start_phase("unknown")
        result = self.launch(session_dir, "implementation", "impl")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("got 'implementation'", result.stderr)

    def test_every_seat_records_its_own_pass_and_stamps_its_role(self) -> None:
        session_dir = self.start_phase("team")
        for kind, slot in (("impl", "impl"), ("impl", "test")):
            result = self.launch(session_dir, kind, slot)
            self.assertEqual(result.returncode, 0, result.stderr)

        # Two records, keyed by seat rather than by what each is doing: the
        # `test` seat opened as a second writer and is still slot `test`.
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
        message = (session_dir / "follow_message.txt").read_text(encoding="utf-8")
        self.assertIn("Write the retry path.", message)
        self.assertIn("impl_summary_impl.txt", message)
        self.assertIn("post", message)
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn(f"follow-up to {name}", board)
        self.assertIn("launcher:", board)
        self.assertIn("done:", board)

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

        followed = self.mesh_launch(
            session_dir, to=name, kind="review", subtask="impl"
        )

        self.assertEqual(followed.returncode, 0, followed.stderr)
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        self.assertEqual(roster[name]["role"], "review")

    def test_compaction_threshold_must_be_configured_as_a_positive_integer(self) -> None:
        configured = self.config_file.read_text(encoding="utf-8")
        key = "PLAN_DELEGATE_COMPACT_ABOVE_TOKENS"
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
            env=environment, capture_output=True, text=True, check=False, timeout=120,
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
        unknown = self.mesh_launch(session_dir, to="missing-seat")
        self.assertEqual(unknown.returncode, 2, unknown.stderr)
        self.assertEqual(status_file.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(self.open_passes(session_dir), before)

        roster[name]["status"] = "running"
        _ = roster_path.write_text(json.dumps(roster), encoding="utf-8")
        busy = self.mesh_launch(session_dir, to=name)
        self.assertEqual(busy.returncode, 2, busy.stderr)
        self.assertEqual(status_file.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(self.open_passes(session_dir), before)

    def test_follow_up_rejects_wrong_slot_and_changed_model(self) -> None:
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
        changed_model = self.mesh_launch(session_dir, to=name)
        self.assertEqual(changed_model.returncode, 2, changed_model.stderr)
        self.assertEqual(self.open_passes(session_dir), before)

    def test_live_claim_refuses_a_second_launcher_without_a_pass(self) -> None:
        session_dir = self.start_phase("claimed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        _ = (session_dir / "impl_claim_impl.json").write_text(json.dumps({
            "pid": os.getpid(), "seat": name, "turn_id": "",
        }), encoding="utf-8")
        before = self.open_passes(session_dir)

        second = self.mesh_launch(session_dir, to=name)

        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertEqual(self.open_passes(session_dir), before)
        self.assertEqual(
            (session_dir / "impl_claim_impl.json").read_text(encoding="utf-8"),
            json.dumps({"pid": os.getpid(), "seat": name, "turn_id": ""}),
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
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if claim_path.exists():
                            claim = cast("dict[str, object]", json.loads(
                                claim_path.read_text(encoding="utf-8")
                            ))
                            if stage == "before" and claim.get("turn_id") == "":
                                break
                            if stage == "after" and roster[name]["status"] == "running":
                                break
                        time.sleep(0.05)
                        try:
                            roster = cast("dict[str, dict[str, object]]", json.loads(
                                roster_path.read_text(encoding="utf-8")
                            ))
                        except json.JSONDecodeError:
                            continue
                    else:
                        self.fail(f"launcher never held a {stage} claim")
                finally:
                    os.killpg(process.pid, signal.SIGKILL)
                    _ = process.wait(timeout=5)

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
                                 text=True, check=False, timeout=30)

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(status_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.open_passes(session_dir), passes)
        self.assertNotIn("follow-up to", (session_dir / "board.log").read_text())

    def test_new_launch_keeps_heartbeat_watcher_output(self) -> None:
        watcher = self.root / "scripts" / "agents" / "heartbeat_watch.sh"
        _ = watcher.write_text(
            "#!/usr/bin/env bash\nprintf 'heartbeat watcher output\\n'\n",
            encoding="utf-8",
        )
        agent = self.root / "scripts" / "agents" / "agent_exec.sh"
        _ = agent.write_text(
            agent.read_text(encoding="utf-8").replace(
                "set -euo pipefail", "set -euo pipefail\nsleep 0.2"
            ), encoding="utf-8",
        )
        session_dir = self.start_phase("heartbeat-visible")

        result = self.launch(session_dir, "impl", "impl")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("heartbeat watcher output", result.stdout)

    def test_failed_seat_with_live_turn_refuses_before_recording_a_pass(self) -> None:
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
        transcript = self.claude_seat(session_dir)

        result = self.claude_follow(session_dir, transcript, "complete")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")
        self.assertIn("impl_summary_impl.txt", (
            self.root / "delivered_message.txt"
        ).read_text(encoding="utf-8"))

    def test_claude_queued_send_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-queued")
        transcript = self.claude_seat(session_dir)

        result = self.claude_follow(session_dir, transcript, "queued")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(transcript.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")
        self.assertIn("blocked:", (
            session_dir / "board.log"
        ).read_text(encoding="utf-8"))

    def test_claude_seat_gone_after_delivery_is_a_worker_error(self) -> None:
        session_dir = self.start_phase("claude-gone")
        transcript = self.claude_seat(session_dir)

        result = self.claude_follow(session_dir, transcript, "gone")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")

    def test_claude_follow_retries_unreadable_agents_listing(self) -> None:
        session_dir = self.start_phase("claude-unreadable")
        transcript = self.claude_seat(session_dir)

        result = self.claude_follow(session_dir, transcript, "unreadable_once")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "completed")

if __name__ == "__main__":
    _ = unittest.main()
