"""Share assignment setup with the recorder and loopback server."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct import assign
from scripts.direct.recorder_fixture import communicate_launcher, invoke_recorder
from scripts.lib.git_test_support import commit_repository
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production.cli_fixture import invoke_cli
from scripts.production import staff
from scripts.production.staff_test_support import fixture_worker_records


ROOT = Path(__file__).resolve().parents[2]
ASSIGN = ROOT / "scripts/direct/assign.sh"


@dataclass(frozen=True)
class InProcessAssignment:
    """Call the assignment entry point in this process."""


@dataclass(frozen=True)
class LauncherScript:
    """Run an assignment shell script in a separate process."""

    path: Path


AssignmentExecution = InProcessAssignment | LauncherScript
DEFAULT_ASSIGNMENT_EXECUTION = InProcessAssignment()


def read_object(path: Path) -> dict[str, object]:
    with path.open() as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        records = cast(dict[str, object], json.load(handle))
    if path.name == codex_mesh.ROSTER_FILE:
        records = fixture_worker_records(records, staff.local_snapshot())
        assignments = codex_mesh.task_assignments()
        for raw in records.values():
            entry = cast(dict[str, object], raw)
            role = codex_mesh.task_role(str(entry.get("thread_id", "")), assignments)
            if isinstance(role, codex_mesh.AssignedWorkerRole):
                entry["role"] = role.role
    return records


class AssignLauncherFixture(mesh_fixture.MeshCommandFixture):
    project: Path = Path()
    history: Path = Path()
    paths: Path = Path()
    prompt: Path = Path()
    registry: Path = Path()
    hang_guard: LauncherHangGuard  # pyright: ignore[reportUninitializedInstanceVariable]
    claims: list[dict[str, object]]
    reservations: list[dict[str, object]]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.claims = []
        self.reservations = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.lifecycle_clock.freeze()
        self.hang_guard = LauncherHangGuard(self.id())
        self.server.hang_guard = self.hang_guard
        self.project = self.session_dir.parent / "project"
        self.project.mkdir()
        owner = staff.by_id(staff.StaffId(os.environ["CODEX_THREAD_ID"]))
        assert owner is not None
        parent = staff.local_snapshot().parent_of(owner)
        assert parent is not None
        _ = staff.rename(parent.id, "fixture-production")
        owner = staff.rename(owner.id, "fixture")
        _ = staff.set_worktree(owner.id, staff.PresentWorktree("natedev", self.project), expected=owner.worktree)
        _ = (self.project / "plan.md").write_text("## Delegation Context\n\n- **Project:** Assignment fixture\n")
        self.history = self.session_dir.parent / "history"
        self.paths = self.session_dir.parent / "paths.txt"
        _ = self.paths.write_text("src/worker.py\ntests/test_worker.py\n")
        self.prompt = self.session_dir.parent / "assignment.txt"
        self.write_phase_prompt("7", "Assignment core", "Implement the assigned change.")
        self.registry = self.session_dir.parent / "models.conf"
        _ = write_registry(self.registry, {'codex.agents': {'gpt-kept-sol': 'low,medium,high,xhigh,max', 'gpt-other-sol': 'low,medium,high,xhigh,max'}, 'assignments': {'direct': 'codex'}, 'direct.codex': {'impl': 'gpt-kept-sol:high', 'test': 'gpt-kept-sol:high', 'fix': 'gpt-kept-sol:high', 'review': 'gpt-kept-sol:high'}})
        checkpoint = self.session_dir.parent / "catalog-current"
        checkpoint.touch()
        self.patches.enter_context(patch.dict(os.environ, {
            "UNIT_WORKERS_STATE": str(self.session_dir.parent / "unit-workers"),
            "UNIT_HISTORY_DIR": str(self.history),
            "UNIT_ACTIVE_DIR": str(self.session_dir.parent / "active"),
            **config_environment(self.session_dir.parent),
            "MODELS_CONFIG_FILE": str(self.registry), "MODELS_CONFIG_PUBLISH": "0",
            "CODEX_CATALOG_SYNC_STATE_FILE": str(checkpoint),
            "NOTIFIER_SESSIONS_DIR": str(self.session_dir.parent / "sessions"),
            "SHUTDOWN_STATE_DIR": str(self.session_dir.parent / "shutdown"),
            "UNIT_SYNC_WRITES": "0",
            "PYTHONPATH": str(ROOT), "TMPDIR": "/tmp",
        }))
        (self.session_dir / "progress_history_state.json").unlink(missing_ok=True)
        self.record("start-run", "--working-dir", str(self.project), "--plan-doc", "plan.md",
                    "--main-family", "codex", "--main-model", "gpt-main", "--main-effort", "high",
                    "--main-session-id", owner.current_session)
        self.record("set-unit", "--production", "fixture-production", "--unit", "fixture-unit")
        self.record("start-phase", "--phase-id", "7", "--phase-title", "Assignment core")
        self.claims = []
        self.reservations = []
        original = self.server.respond

        @self.hang_guard.callback
        def observe(request: dict[str, object]) -> list[dict[str, object]] | None:
            roster_path = self.session_dir / codex_mesh.ROSTER_FILE
            if roster_path.exists() and roster_path.stat().st_size:
                for value in read_object(roster_path).values():
                    record = cast(dict[str, object], value)
                    if record.get("allocation_pending"):
                        owner = record.get("allocation_owner_pid")
                        if isinstance(owner, int):
                            os.kill(owner, 0)
                        self.reservations.append(record.copy())
            if request.get("method") == "turn/start":
                self.claims.extend(cast(dict[str, object], entry).copy() for entry in
                                   read_object(self.session_dir / codex_mesh.ROSTER_FILE).values())
            frames = original(request)
            params = cast(dict[str, object], request.get("params", {}))
            thread = params.get("threadId")
            if frames is not None and isinstance(thread, str) and thread != mesh_fixture.THREAD_ID:
                return cast(list[dict[str, object]], json.loads(
                    json.dumps(frames).replace(mesh_fixture.THREAD_ID, thread)))
            return frames

        self.server.respond = observe

    def write_phase_prompt(self, phase: str, title: str, task: str = "Review the assigned change.") -> None:
        _ = self.prompt.write_text(f"Phase {phase} — {title}\n{task}\n")

    def record(self, *arguments: str) -> None:
        result = invoke_recorder([arguments[0], "--session-dir", str(self.session_dir), *arguments[1:]],
                                 os.environ.copy(), self.project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def launch(self, role: str = "implement", *options: str,
               paths: bool = True, session: bool = True,
               execution: AssignmentExecution = DEFAULT_ASSIGNMENT_EXECUTION) -> subprocess.CompletedProcess[str]:
        arguments = ["--role", role, "--cwd", str(self.project),
                     "--phase", "7", "--prompt", str(self.prompt)]
        if session:
            arguments.extend(["--session-dir", str(self.session_dir)])
        if paths and role in {"implement", "test", "fix"}:
            arguments.extend(["--paths", str(self.paths)])
        arguments.extend(options)
        if isinstance(execution, InProcessAssignment):
            return invoke_cli(assign, assign.main, arguments, os.environ.copy(), ROOT)
        return self.run_launcher(["bash", str(execution.path), *arguments])

    def run_launcher(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        with subprocess.Popen(arguments, env=os.environ.copy(), cwd=ROOT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True) as child:
            output, errors = communicate_launcher(child)
            return subprocess.CompletedProcess(arguments, child.returncode, output, errors)

    def successful(self, role: str = "implement", *options: str,
                   execution: AssignmentExecution = DEFAULT_ASSIGNMENT_EXECUTION) -> subprocess.CompletedProcess[str]:
        result = self.launch(role, *options, execution=execution)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def review_launcher(self, option: str, name: str, pass_number: str) -> None:
        result = self.run_launcher([
            "bash", str(ROOT / "scripts/direct/review.sh"), option, name, "--phase", "7",
            str(self.session_dir), str(self.project), str(self.prompt), "review", "Code review",
            "Reviewing assigned changes", pass_number, "contract",
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assignment_records(self) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], read_object(self.session_dir / codex_mesh.ROSTER_FILE))

    def identity_path(self, name: str) -> Path:
        reference = self.assignment_records()[name]["staff_id"]
        return next(path for path in self.session_dir.glob("impl_worker_*.json")
                    if read_object(path).get("staff_id") == reference)

    def commit_project(self, message: str = "Initial fixture") -> None:
        _ = subprocess.run(["git", "init", "-q"], cwd=self.project, check=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        _ = commit_repository(self.project, message)

    def prepare_style_project(self) -> None:
        self.commit_project()
        config = self.session_dir.parent / "lint.conf"
        _ = write_config_fixture(config, "style_review=on\n")
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {"LINT_CONFIG_FILE": str(config), "CONF_DIR": str(Path(config).parent)})))

    def events(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in
                (self.history / "runs" / f"{self.session_dir.name}.jsonl").read_text().splitlines()]

    def prepare_launcher(self) -> tuple[Path, Path]:
        # Allocate only the folder here; the phase and dispatch helpers stay real.
        tree = self.session_dir.parent / "launcher/scripts"
        direct = tree / "direct"
        direct.mkdir(parents=True)
        for source in (ROOT / "scripts/direct").iterdir():
            if source.is_file() and source.name not in {"assign.sh", "assign.py", "prepare_session.sh"}:
                (direct / source.name).symlink_to(source)
        for directory in ("agents", "lib", "production"):
            (tree / directory).symlink_to(ROOT / "scripts" / directory, target_is_directory=True)
        _ = shutil.copy2(ASSIGN, direct / "assign.sh")
        coordinator = ASSIGN.with_suffix(".py")
        if coordinator.exists():
            _ = shutil.copy2(coordinator, direct / "assign.py")
        prepared = self.session_dir.parent / "prepare-called"
        prepare = direct / "prepare_session.sh"
        _ = prepare.write_text(f"#!/bin/sh\ntouch '{prepared}'\nprintf '%s\\n' '{self.session_dir}'\n")
        prepare.chmod(0o755)
        return direct / "assign.sh", prepared
