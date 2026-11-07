"""Share assignment setup with the recorder and loopback server."""
from __future__ import annotations

import fcntl
import json
import os
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct import assign
from scripts.direct.recorder_fixture import communicate_launcher, invoke_recorder
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.git_test_support import commit_repository
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.lib.server_test_support import owned_server_record
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import staff, unit_config
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_worker_records


ROOT = Path(__file__).resolve().parents[2]
ASSIGN = ROOT / "scripts/direct/assign.sh"


def backend_fixture_command(directory: Path, command: list[str],
                            environment: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """Test a backend beneath the actual assign process."""
    directory.mkdir(parents=True, exist_ok=True)
    startup = directory / "sitecustomize.py"
    _ = startup.write_text("\n".join((
        "import os, subprocess, sys",
        "def enter(frame, event, argument):",
        f"    if event == 'call' and frame.f_code.co_filename == {str(ASSIGN.with_suffix('.py'))!r} and frame.f_code.co_name == 'main':",
        "        sys.setprofile(None)",
        "        environment = dict(os.environ)",
        "        environment['PYTHONPATH'] = environment.pop('FIXTURE_ASSIGN_PYTHONPATH')",
        f"        raise SystemExit(subprocess.call({command!r}, env=environment))",
        f"if sys.argv[0] == {str(ASSIGN.with_suffix('.py'))!r}: sys.setprofile(enter)",
    )) + "\n")
    child_environment = dict(environment)
    child_environment["FIXTURE_ASSIGN_PYTHONPATH"] = environment.get("PYTHONPATH", str(ROOT))
    child_environment["PYTHONPATH"] = os.pathsep.join((str(directory), str(ROOT)))
    return ["bash", str(ASSIGN)], child_environment


@dataclass(frozen=True)
class InProcessAssignment:
    """Call the assignment entry point in this process."""


@dataclass(frozen=True)
class LauncherScript:
    """Run an assignment shell script in a separate process."""

    path: Path


AssignmentExecution = InProcessAssignment | LauncherScript
# Worker admission requires the actual assignment process.
DEFAULT_ASSIGNMENT_EXECUTION = LauncherScript(ASSIGN)


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
        write_text_atomic(self.session_dir / "worker_transport", "mesh\n")
        _ = self.enterContext(owned_server_record(self.session_dir / "mesh_server.json"))
        self.lifecycle_clock.freeze()
        self.hang_guard = LauncherHangGuard(self.id())
        self.server.hang_guard = self.hang_guard
        self.project = self.session_dir.parent / "project"
        self.project.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(self.project)], check=True,
                           capture_output=True, timeout=TEST_HANG_GUARD_SECS)
        _ = unit_config.write(self.project, unit_config.UnitConfig(
            unit_config.PlanFile(Path("plan.md")), unit_config.NoAppPort(), ()))
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
        self.record("set-unit", "--production", "fixture-production", "--unit", "fixture")
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

    def run_launcher(self, arguments: list[str], *, family: str = "codex",
                     isolate_ancestors: bool = False) -> subprocess.CompletedProcess[str]:
        director = self.session_dir.parent / family
        if not director.exists():
            director.symlink_to(sys.executable)
        completion = self.session_dir.parent / "launcher-exit"
        script = "import subprocess, sys; raise SystemExit(subprocess.call(sys.argv[1:]))"
        if isolate_ancestors:
            completion.unlink(missing_ok=True)
            script = "\n".join((
                "import os, select, subprocess, sys",
                "from pathlib import Path",
                "starter = os.pidfd_open(os.getpid())",
                "if os.fork(): os._exit(0)",
                # The pidfd becomes readable after the kernel reparents the child.
                "select.select([starter], [], [])",
                "os.close(starter)",
                f"Path({str(completion)!r}).write_text(str(subprocess.call(sys.argv[1:])))",
            ))
        command = [str(director), "-c", script, *arguments]
        with subprocess.Popen(command, env=os.environ.copy(), cwd=ROOT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              start_new_session=True) as child:
            output, errors = communicate_launcher(child)
            if isolate_ancestors:
                self.assertTrue(completion.exists(), output + errors)
            code = int(completion.read_text()) if isolate_ancestors else child.returncode
            return subprocess.CompletedProcess(arguments, code, output, errors)

    def successful(self, role: str = "implement", *options: str,
                   execution: AssignmentExecution = DEFAULT_ASSIGNMENT_EXECUTION) -> subprocess.CompletedProcess[str]:
        result = self.launch(role, *options, execution=execution)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def review_launcher(self, option: str, name: str, pass_number: str) -> None:
        _ = self.successful("review", option, name, "--lens", "contract", "--pass", pass_number)

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

    def instrument_launcher(self, code: str) -> None:
        """Instrument boundaries inside the actual coordinator process."""
        directory = self.session_dir.parent / "instrument"
        directory.mkdir(exist_ok=True)
        injection = directory / "injection.py"
        _ = injection.write_text(code.replace("from scripts.direct import assign",
                                               "import sys; assign = sys.modules['__main__']"))
        _ = (directory / "sitecustomize.py").write_text("\n".join((
            "import runpy, sys",
            "def enter(frame, event, arg):",
            f"    if event == 'call' and frame.f_code.co_filename == {str(ASSIGN.with_suffix('.py'))!r} and frame.f_code.co_name == 'main':",
            "        sys.setprofile(None)",
            f"        runpy.run_path({str(injection)!r})",
            f"if sys.argv[0] == {str(ASSIGN.with_suffix('.py'))!r}: sys.setprofile(enter)",
        )))
        self.patches.enter_context(patch.dict(os.environ, {"PYTHONPATH": f"{directory}{os.pathsep}{ROOT}"}))

    def prepare_launcher(self) -> Path:
        prepared = self.session_dir.parent / "prepare-called"
        self.instrument_launcher("\n".join((
            "import subprocess",
            "from pathlib import Path",
            "from scripts.direct import assign",
            "real_call = assign.call",
            "def call(command, env, **options):",
            "    if command[1] == str(assign.SCRIPTS / 'prepare_session.sh'):",
            f"        Path({str(prepared)!r}).touch()",
            f"        return subprocess.CompletedProcess(command, 0, {str(self.session_dir)!r} + '\\n', '')",
            "    return real_call(command, env, **options)",
            "assign.call = call",
        )))
        return prepared
