"""Test Claude launcher arguments, staff identity, and pass records."""

from __future__ import annotations

from scripts.lib import test_hang_guard
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file

from scripts.lib.test_models_registry import registry_text, write_registry

from functools import cached_property
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast, override

from scripts.direct.recorder_fixture import LauncherFixture, configure_launcher_fixture, launcher_environment, launcher_registry, prepare_launcher_runtime

if TYPE_CHECKING:
    from ..lint.memory_test_support import config_environment, write_config_fixture
else:
    from scripts.lint.memory_test_support import config_environment, write_config_fixture


DIRECT_DIR = Path(__file__).parent
AGENTS_DIR = DIRECT_DIR.parent / "agents"



CLAUDE_REGISTRY = registry_text({'assignments': {'direct': 'claude'}, 'direct.claude': {'impl': 'opus:high', 'test': 'opus:high', 'fix': 'opus:high'}, 'claude.agents': {'opus': 'low,medium,high,xhigh,max'}})

STUB_CLAUDE = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ['STUB_CLAUDE_ROOT'])
if sys.argv[1] == '--bg':
    settings = json.loads(sys.argv[sys.argv.index('--settings') + 1])['env']
    session = pathlib.Path(settings['UNIT_RUN_DIR'])
    baseline = session / 'phases/3/phase_baseline'
    if baseline.exists():
        (session / 'baseline-at-worker-open').write_bytes(baseline.read_bytes())
    if os.environ.get('TEST_WORKER_STARTED'):
        pathlib.Path(os.environ['TEST_WORKER_STARTED']).touch()
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
    session_id = 'abc12345-' + str(uuid.uuid5(uuid.NAMESPACE_URL, name))[9:]
    projects = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or str(root / '.claude')) / 'projects/fixture'
    projects.mkdir(parents=True, exist_ok=True)
    (projects / f'{session_id}.jsonl').write_text(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'end_turn'}}) + '\\n')
    registry = pathlib.Path(os.environ.get('NOTIFIER_SESSIONS_DIR', str(root / 'sessions')))
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{os.environ['FIXTURE_BACKEND_PID']}.json").write_text(json.dumps({
        'id': 'abc12345', 'sessionId': session_id,
        'name': name, 'pid': int(os.environ['FIXTURE_BACKEND_PID']),
        'procStart': os.environ['FIXTURE_BACKEND_START'], 'updatedAt': 1, 'messagingSocketPath': ''}))
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
    session_id = json.loads(next((root / 'sessions').glob('*.json')).read_text())['sessionId']
    print(json.dumps([{'id': 'abc12345', 'sessionId': session_id, 'name': name, 'status': status}]
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
transcript = pathlib.Path(os.environ['STUB_TRANSCRIPT'])
from scripts.production import staff, session_staff
rows = staff.local_snapshot()
worker = rows.by_current_session(transcript.name.removesuffix('.jsonl'))
(root / 'follow_cwd.txt').write_text(str(session_staff.worker_directory(rows, worker.id)))
(root / 'delivered_message.txt').write_text(pathlib.Path(sys.argv[sys.argv.index('--file') + 1]).read_text())
behavior = os.environ.get('STUB_SEND_BEHAVIOR', 'complete')
if behavior == 'queued':
    print('QUEUED')
    sys.exit(0)
with transcript.open('a') as output:
    output.write(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'end_turn'}}) + '\\n')
pathlib.Path(os.environ['STUB_SUMMARY']).write_text('Claude follow-up summary\\n')
if behavior == 'gone':
    (root / 'worker_state').write_text('gone\\n')
if behavior == 'unreadable_once':
    (root / 'unreadable_listing_once').write_text('yes\\n')
print('SENT: fixture')
"""


class OpenPass(TypedDict):
    worker_name: str
    kind: str
    status: str


# "pass" is a keyword, so the recorder's state key needs the functional form.
ProgressState = TypedDict("ProgressState", {"pass": dict[str, OpenPass]})


MODELS_REGISTRY = launcher_registry(("impl", "test", "fix"))


class ImplementLauncherFixture(LauncherFixture):
    """Every worker carries a kind, and the board line says which."""

    implement_script: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.prompt_file: Path = self.root / "implementation_prompt.md"
        self.implement_script = self.root / "scripts" / "direct" / "implement.sh"
        _ = self.staff_parent

    @cached_property
    def staff_parent(self) -> str:
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership
        with isolated_staff_file(self.root / "staff.json", {}):
            parent = unit_membership("fixture", "fixture-unit")
            from scripts.production import staff
            parent = staff.set_worktree(parent.id, staff.PresentWorktree("natedev", self.working_dir), expected=parent.worktree)
        return parent.id

    @override
    def prepare_fixture(self) -> None:
        self.prepare_project(
            "feature/workers", "implementation_prompt.md", "Write the retry path.\n",
            (
                "# whole: 1..", "UNIT_PROGRESS_INTERVAL_SECONDS=180",
                "# whole: 1..", "UNIT_COMPACT_ABOVE_TOKENS=100000",
                "# whole: 1..", "MAX_FIX_ATTEMPTS=3", "# whole: 1..", "MAX_REOPENS=3", "# whole: 1..", "STALLED_ROUNDS=3",
                "# whole: 1..", "RUNAWAY_ROUNDS=10", "# range: 5e-324..", "REPAIR_ROUNDS_PER_FINDING=2",
                "# whole: 1..", "MIN_REPAIR_BUDGET=2", "# whole: 1..", "MAX_CONSECUTIVE_SAME_KIND_PASSES=6",
                "# whole: 0..", "MAX_REVIEW_CANCELLATIONS=1", "# whole: 1..", "ROUTING_FROM_ROUND=4",
                "# whole: 1..", "ASTRA_AFTER_FAILED_FIXES=2", "",
            ), CLAUDE_REGISTRY,
        )
        _ = write_config_fixture(self.config_file, self.config_file.read_text(encoding="utf-8"))
        _ = config_environment(self.root)
        # A phase launch captures HEAD before opening its worker, as a real checked-out phase does.
        _ = subprocess.run(["git", "-C", str(self.working_dir), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty",
                            "-m", "base"], check=True, capture_output=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude2.md").write_text("---\nlogin: worker@example.invalid\n---\n")
        _ = (self.root / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "worker@example.invalid"},
        }))
        self.implement_script = self.build_script_tree()

    @override
    def start_phase(self, name: str) -> Path:
        session = super().start_phase(name)
        state_file = session / "progress_history_state.json"
        state = cast(dict[str, object], json.loads(state_file.read_text()))
        state["owner_staff_id"] = self.staff_parent
        _ = state_file.write_text(json.dumps(state))
        return session

    def build_script_tree(self) -> Path:
        """Copy the real wrapper beside a stub agent, so nothing calls a model."""
        direct_dir, agents, lib = prepare_launcher_runtime(self.root)
        # These tests cover the launcher after admission. Assign tests cover admission.
        _ = (direct_dir / "assign_gate.py").write_text("# Admission succeeds in this private fixture.\n")
        for name in ("conf.py", "conf.sh"):
            _ = shutil.copy2(DIRECT_DIR.parent / "lib" / name, lib / name)
        _ = shutil.copy2(DIRECT_DIR.parent / "lib/nix_env.py", lib / "nix_env.py")
        home_lib = self.root / ".claude/scripts/lib"
        home_lib.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(DIRECT_DIR.parent / "lib/py", home_lib / "py")
        configure_launcher_fixture(direct_dir / "implement.sh")
        _ = shutil.copy2(AGENTS_DIR / "claude_account.sh", agents / "claude_account.sh")
        claude = self.root / "claude-bin"
        _ = claude.write_text(STUB_CLAUDE, encoding="utf-8")
        claude.chmod(0o755)
        sender = self.root / "send"
        _ = sender.write_text(STUB_SEND, encoding="utf-8")
        sender.chmod(0o755)
        _ = shutil.copy2(DIRECT_DIR.parent / "production/worker_fill.py", self.root / "scripts/production/worker_fill.py")
        opening_sender = self.root / "opening-send.py"
        _ = opening_sender.write_text("import json, os, pathlib, sys\n"
            + "root = pathlib.Path(os.environ['STUB_CLAUDE_ROOT'])\n"
            + "rows = json.loads(pathlib.Path(os.environ['STAFF_FILE']).read_text())['rows']\n"
            + "(root / 'staff-at-work.json').write_text(json.dumps(rows))\n"
            + "(root / 'opening-message.txt').write_text(pathlib.Path(sys.argv[sys.argv.index('--file') + 1]).read_text())\n"
            + "print('SENT: fixture')\n")
        agent_bg = agents / "agent_bg.sh"
        _ = shutil.copy2(AGENTS_DIR / "agent_bg.sh", agent_bg)
        _ = agent_bg.write_text(
            agent_bg.read_text(encoding="utf-8").replace(
                'POLL_SECS="${7:-15}"', 'POLL_SECS="${7:-0.05}"'
            ), encoding="utf-8",
        )
        return direct_dir / "implement.sh"

    @override
    def environment(self) -> dict[str, str]:
        environment = launcher_environment(self.root)
        environment["CONF_DIR"] = str(self.root)
        environment["CLAUDE_BIN"] = str(self.root / "claude-bin")
        environment["STUB_CLAUDE_ROOT"] = str(self.root)
        environment["NOTIFIER_SESSIONS_DIR"] = str(self.root / "sessions")
        environment["STAFF_FILE"] = str(self.root / "staff.json")
        environment["CODEX_THREAD_ID"] = self.staff_parent
        _ = environment.pop("STAFF_ID", None)
        from scripts.message import sessions
        environment["FIXTURE_BACKEND_PID"] = str(os.getpid())
        environment["FIXTURE_BACKEND_START"] = sessions.process_start(os.getpid()) or ""
        environment["AGENT_BG_SEND_SCRIPT"] = str(self.root / "opening-send.py")
        environment["AGENT_NOTES_DIR"] = str(self.root / "notes")
        # The copied shell helper uses the real account policy, while identity
        # and configuration stay in this fixture's home.
        environment["PYTHONPATH"] = str(DIRECT_DIR.resolve().parents[1])
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                     "CLAUDE_CODE_REMOTE_MEMORY_DIR", "CODEX_HOME"):
            _ = environment.pop(name, None)
        environment["TEST_WAIT_HELPER"] = str(DIRECT_DIR.resolve() / "wait_for_test_state.py")
        environment["TEST_HEARTBEAT_READY"] = str(self.root / "heartbeat-ready")
        return environment

    def launch(
        self, session_dir: Path, kind: str, slot: str
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        environment["STUB_SUMMARY"] = str(session_dir / f"impl_summary_{slot}.txt")
        return subprocess.run(
            [
                "bash", str(self.implement_script), str(session_dir), str(self.working_dir),
                str(self.prompt_file), kind, "the retry path", kind,
                "writing the retry path", "0", slot,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def checked_launch(
        self, session: Path, *options: str, kind: str = "impl",
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        _ = environment.pop("TEST_LEGACY_LAUNCH", None)
        environment["TEST_WORKER_STARTED"] = str(session / "worker-started")
        environment["STUB_SUMMARY"] = str(session / "phases" / "3" / "impl_summary_impl.txt")
        return subprocess.run(
            ["bash", str(self.implement_script), *options, str(session), str(self.working_dir),
             str(self.prompt_file), kind, "the retry path", kind, "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=environment, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
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
        environment = self.environment()
        environment["PATH"] = str(binaries) + os.pathsep + environment["PATH"]
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        return environment, calls

    def worker_launch(
        self, session_dir: Path, *, to: str = "", slot: str = "impl",
        kind: str = "impl", fail: bool = False, resolves_round: bool = False,
        fix_cause: str = "",
    ) -> subprocess.CompletedProcess[str]:
        arguments = ["bash", str(self.implement_script)]
        if to:
            arguments.extend(("--to", to))
        if fix_cause:
            arguments.extend(("--fix-cause", fix_cause))
        arguments.extend((str(session_dir), str(self.working_dir), str(self.prompt_file),
                          "impl", "the retry path", kind, "writing the retry path", "0", slot))
        environment = self.environment()
        environment["STUB_SUMMARY"] = str(session_dir / f"impl_summary_{slot}.txt")
        if to:
            member = self.recorded_worker(session_dir, slot)
            transcript = next((self.root / ".claude/projects").rglob(f"{member.current_session}.jsonl"))
            environment.update({"STUB_TRANSCRIPT": str(transcript),
                                "AGENT_BG_SEND_SCRIPT": str(self.root / "send"),
                                "STUB_SEND_BEHAVIOR": "queued" if fail else "complete"})
        if resolves_round:
            environment["UNIT_RESOLVES_ROUND"] = "1"
        return subprocess.run(arguments, check=False, capture_output=True, text=True,
                              env=environment, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

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
                timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
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

    def recorded_worker(self, session_dir: Path, slot: str = "impl") -> staff.StaffRow:
        identity = cast(dict[str, object], json.loads((session_dir / f"impl_worker_{slot}.json").read_text()))
        with isolated_staff_file(self.root / "staff.json", {}):
            member = staff.local_snapshot().by_id(staff.native_id(str(identity["staff_id"])))
        assert member is not None
        return member

    def claude_worker(self, session_dir: Path) -> Path:
        registry = self.root / ".claude" / "config" / "models.conf"
        _ = write_registry(registry, base_text=CLAUDE_REGISTRY)
        sessions = self.root / "sessions"
        sessions.mkdir()
        projects = self.root / ".claude" / "projects" / "fixture"
        projects.mkdir(parents=True)
        session_id = "abc12345-2222-4333-8444-555555555555"
        _ = (sessions / f"{os.getpid()}.json").write_text(json.dumps({
            "id": "abc12345", "sessionId": session_id, "name": "fixture-impl",
            "pid": os.getpid(), "procStart": self.environment()["FIXTURE_BACKEND_START"], "updatedAt": 1,
            "messagingSocketPath": "",
        }), encoding="utf-8")
        transcript = projects / f"{session_id}.jsonl"
        _ = transcript.write_text(
            json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn"}})
            + "\n", encoding="utf-8"
        )
        from scripts.production import staff
        from scripts.production.staff_test_support import fixture_worker, isolated_staff_file
        with isolated_staff_file(self.root / "staff.json", {
            "CLAUDE_CONFIG_DIR": str(projects.parent.parent),
        }):
            worker = fixture_worker("fixture-impl", staff.native_id(self.staff_parent), runtime_id=session_id)
        _ = (session_dir / "impl_worker_impl.json").write_text(json.dumps({
            "staff_id": worker.id, "slot": "impl", "model": "opus",
        }), encoding="utf-8")
        _ = (session_dir / "impl_status_impl").write_text(
            "implemented\n", encoding="utf-8"
        )
        _ = (self.root / "worker_state").write_text("idle\n", encoding="utf-8")
        for name, content in (("claude-bin", STUB_CLAUDE), ("send", STUB_SEND)):
            script = self.root / name
            _ = script.write_text(content, encoding="utf-8")
            script.chmod(0o755)
        return transcript

    def claude_follow(
        self, session_dir: Path, transcript: Path, behavior: str
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        environment.update({
            "CLAUDE_BIN": str(self.root / "claude-bin"),
            "AGENT_BG_SEND_SCRIPT": str(self.root / "send"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CLAUDE_PROJECTS_DIR": str(transcript.parent.parent),
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
                check=False, capture_output=True, text=True, env=environment, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
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
        registry = self.root / ".claude" / "config" / "models.conf"
        _ = write_registry(registry, {"direct.claude": {"impl": registry_row}}, base_text=CLAUDE_REGISTRY)
        stub = self.root / "claude-bin"
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
            check=False, capture_output=True, text=True, env=environment, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def open_passes(self, session_dir: Path) -> dict[str, OpenPass]:
        stored = cast(
            "ProgressState",
            json.loads((session_dir / "progress_history_state.json").read_text(encoding="utf-8")),
        )
        return stored["pass"]
