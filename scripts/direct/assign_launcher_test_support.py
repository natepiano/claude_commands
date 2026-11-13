"""Private native workers for assignment command tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO, cast, override
from unittest.mock import patch

from scripts.direct import assign, phase_table
from scripts.direct.recorder_fixture import communicate_launcher, invoke_recorder
from scripts.lib.git_test_support import commit_repository
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import staff, unit_config, test_worker, worker
from scripts.production.cli_fixture import invoke_cli
from scripts.agents import remote

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
    child_environment["FIXTURE_ASSIGN_PYTHONPATH"] = environment.get("PYTHONPATH", str(ROOT / "scripts/lib/python_startup"))
    child_environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "scripts/lib/python_startup"), str(directory)))
    return ["bash", str(ASSIGN)], child_environment


@dataclass(frozen=True)
class InProcessAssignment:
    """Run the real command with private native process boundaries."""


@dataclass(frozen=True)
class LauncherScript:
    path: Path


AssignmentExecution = InProcessAssignment | LauncherScript
DEFAULT_ASSIGNMENT_EXECUTION = InProcessAssignment()


def invoke_assignment(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with patch.object(assign, "require_live_director"):
        return invoke_cli(assign, assign.main, arguments, environment, ROOT)


@contextmanager
def assignment_helpers() -> Generator[None]:
    """Keep helper parsers and handlers in the fixture process."""
    from scripts.direct.assign_claude_test_support import direct_launcher_helpers
    from scripts.direct.findings_test_support import invoke_findings

    cache = Path.home() / ".cache"
    seed_interpreter_cache(cache)
    with patch.dict(os.environ, {"CLAUDE_PYTHON": "", "XDG_CACHE_HOME": str(cache)}), direct_launcher_helpers():
        run_process = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            path = Path(command[1]).resolve() if len(command) > 1 else Path()
            if path not in (ROOT / "scripts/direct/phase_table.py", ROOT / "scripts/direct/findings.py"):
                return run_process(command, **options)
            environment = cast(dict[str, str], options.get("env", os.environ.copy()))
            directory = Path(cast(str | Path, options.get("cwd") or Path.cwd()))
            result = (invoke_findings(command[2:], environment, directory) if path.name == "findings.py" else
                      invoke_cli(phase_table, phase_table.main, command[2:], environment, directory))
            if not options.get("capture_output"):
                if options.get("stdout") is None:
                    print(result.stdout, end="")
                if options.get("stderr") is None:
                    print(result.stderr, end="", file=sys.stderr)
            if options.get("check"):
                result.check_returncode()
            return result

        with patch.object(subprocess, "run", side_effect=run):
            yield


def read_object(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text()))


class AssignLauncherFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.native: test_worker.WorkerCommandTests = test_worker.WorkerCommandTests("runTest")
        self.project: Path = Path()
        self.session_dir: Path = Path()
        self.history: Path = Path()
        self.paths: Path = Path()
        self.prompt: Path = Path()
        self.registry: Path = Path()
        self.home: Path = Path()
        self.hang_guard: LauncherHangGuard = LauncherHangGuard(self.id())
        self.injections: list[str] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.native.setUp()
        config = Path(os.environ["CONF_DIR"]) / "direct.conf"
        _ = config.write_text((ROOT / "config/direct.conf").read_text())
        _ = config.with_name("shutdown.conf").write_text((ROOT / "config/shutdown.conf").read_text())
        self.addCleanup(self.native.doCleanups)
        _ = self.enterContext(assignment_helpers())
        self.home = Path.home()
        self.session_dir = self.native.run_dir
        self.project = self.session_dir.parent / "project"
        self.project.mkdir()
        _ = subprocess.run(["git", "init", "-q", str(self.project)], check=True, capture_output=True,
                           timeout=TEST_HANG_GUARD_SECS)
        _ = unit_config.write(self.project, unit_config.UnitConfig(unit_config.PlanFile(Path("plan.md")),
                                                                unit_config.NoAppPort(), ()))
        owner = staff.by_id(self.native.fixture.parent.id)
        assert owner is not None
        parent = staff.local_snapshot().parent_of(owner)
        assert parent is not None
        _ = staff.rename(parent.id, "fixture-production")
        owner = staff.rename(owner.id, "fixture")
        _ = staff.set_worktree(owner.id, staff.PresentWorktree(remote.local_machine(), self.project), expected=owner.worktree)
        _ = (self.project / "plan.md").write_text("# Assignment fixture\n\n## Delegation Context\n\n- **Project:** Test\n")
        self.history = self.session_dir.parent / "history"
        self.paths = self.session_dir.parent / "paths.txt"
        _ = self.paths.write_text("src/worker.py\ntests/test_worker.py\n")
        self.prompt = self.session_dir.parent / "assignment.txt"
        self.write_phase_prompt("7", "Assignment core", "Implement the assigned change.")
        self.registry = self.native.fixture.root / "conf/models.conf"
        _ = write_registry(self.registry, {'codex.agents': {'gpt-kept-sol': 'low,medium,high,xhigh,max', 'gpt-other-sol': 'low,medium,high,xhigh,max'}, 'assignments': {'direct': 'codex'}, 'direct.codex': {'impl': 'gpt-kept-sol:high', 'test': 'gpt-kept-sol:high', 'fix': 'gpt-kept-sol:high', 'review': 'gpt-kept-sol:high'}})
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "UNIT_HISTORY_DIR": str(self.history), 
            "UNIT_RUN_ROOT": str(self.session_dir.parent), "ROSTER_RUN_ROOT": str(self.session_dir.parent),
            "MODELS_CONFIG_FILE": str(self.registry), "UNIT_SYNC_WRITES": "0", "UNIT_RUN_DIR": str(self.session_dir),
            "PYTHONPATH": str(ROOT / "scripts/lib/python_startup"), "ASTRA_ROLES_STATE": str(self.session_dir.parent / "roles"),
        })))
        for key in ("MODELS_LAUNCH_CONFIGURATION", "MODELS_REGISTRY_EDIT_BASE"):
            _ = os.environ.pop(key, None)
        original = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)
        def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            path = Path(command[1]).resolve() if len(command) > 1 else Path()
            environment = cast(dict[str, str], options.get("env", os.environ.copy()))
            if path == ROOT / "scripts/production/worker.py" and Path(command[0]).resolve() == ROOT / "scripts/lib/py":
                result = invoke_cli(worker, lambda: worker.main(command[2:]), command[2:], environment, self.project)
            elif path == ROOT / "scripts/direct/progress_history.py":
                result = invoke_recorder(command[2:], environment, self.project)
            else:
                return original(command, **options)
            output = options.get("stdout")
            if output is not None:
                stream = cast(TextIO, output)
                _ = stream.write(result.stdout + result.stderr)
            elif not options.get("capture_output"):
                print(result.stdout, end="")
                print(result.stderr, end="", file=sys.stderr)
            return result
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=run))
        self.commit_project()
        (self.session_dir / "progress_history_state.json").unlink()
        self.record("start-run", "--working-dir", str(self.project), "--plan-doc", "plan.md",
                    "--main-family", "codex", "--main-model", "gpt-main", "--main-effort", "high",
                    "--main-session-id", owner.current_session)
        self.record("set-unit", "--production", "fixture-production", "--unit", "fixture")
        self.record("start-phase", "--phase-id", "7", "--phase-title", "Assignment core")

    @property
    def turn_starts(self) -> int:
        return len(self.native.agent.submitted)

    @property
    def turn_inputs(self) -> list[str]:
        return [text for text in (entry[1] for entry in self.native.agent.submitted)]

    def write_phase_prompt(self, phase: str, title: str, task: str = "Review the assigned change.") -> None:
        _ = self.prompt.write_text(f"Phase {phase} — {title}\n{task}\n")

    def record(self, *arguments: str) -> None:
        result = invoke_recorder([arguments[0], "--session-dir", str(self.session_dir), *arguments[1:]],
                                 os.environ.copy(), self.project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def launch(self, role: str = "implement", *options: str, paths: bool = True, session: bool = True,
               execution: AssignmentExecution = DEFAULT_ASSIGNMENT_EXECUTION) -> subprocess.CompletedProcess[str]:
        arguments = ["--role", role, "--cwd", str(self.project), "--phase", "7", "--prompt", str(self.prompt)]
        if session:
            arguments.extend(["--session-dir", str(self.session_dir)])
        if paths and role in {"implement", "test", "fix"}:
            arguments.extend(["--paths", str(self.paths)])
        arguments.extend(options)
        if not any(option in options for option in ("--to", "--new-worker")):
            arguments.extend(["--new-worker", "impl" if role == "implement" else role])
        if isinstance(execution, LauncherScript):
            return self.run_launcher(["bash", str(execution.path), *arguments])
        return invoke_assignment(arguments, os.environ.copy())

    def run_launcher(self, arguments: list[str], *, family: str = "codex",
                     isolate_ancestors: bool = False) -> subprocess.CompletedProcess[str]:
        director = self.session_dir.parent / family
        if not director.exists():
            director.symlink_to(sys.executable)
        command = [str(director), "-c", "import subprocess, sys; raise SystemExit(subprocess.call(sys.argv[1:]))", *arguments]
        if isolate_ancestors:
            command = ["unshare", "--user", "--map-current-user", "--pid", "--fork", "--mount-proc", *command]
        with subprocess.Popen(command, env=os.environ.copy(), cwd=ROOT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as child:
            output, errors = communicate_launcher(child)
            return subprocess.CompletedProcess(arguments, child.returncode, output, errors)

    def successful(self, role: str = "implement", *options: str,
                   execution: AssignmentExecution = DEFAULT_ASSIGNMENT_EXECUTION) -> subprocess.CompletedProcess[str]:
        result = self.launch(role, *options, execution=execution)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assignment_records(self) -> dict[str, dict[str, object]]:
        record = staff.local_snapshot()
        return {row.name: {"staff_id": row.id, "thread_id": row.current_session}
                for row in record.find(kind="worker", parent=self.native.fixture.parent.id)}

    def commit_project(self, message: str = "Initial fixture") -> None:
        _ = commit_repository(self.project, message, allow_empty=True)

    def prepare_style_project(self) -> None:
        self.commit_project()
        config = self.session_dir.parent / "lint.conf"
        _ = write_config_fixture(config, "style_review=on\n")
        _ = config_environment(config.parent)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"LINT_CONFIG_FILE": str(config), "CONF_DIR": str(config.parent)})))

    def events(self) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in
                (self.history / "runs" / f"{self.session_dir.name}.jsonl").read_text().splitlines()]

    def instrument_launcher(self, code: str) -> None:
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
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"PYTHONPATH": f"{ROOT / 'scripts/lib/python_startup'}{os.pathsep}{directory}"})))
        self.injections.append(code)
        saved = vars(assign).copy()
        exec(code, {})
        def restore() -> None:
            for name in saved:
                setattr(assign, name, cast(object, saved[name]))
        self.addCleanup(restore)

    def prepare_launcher(self) -> Path:
        prepared = self.session_dir.parent / "prepare-called"
        original = assign.call
        def call(command: list[str], env: dict[str, str], *, capture: bool = False,
                 check: bool = True) -> subprocess.CompletedProcess[str]:
            if command[1] == str(assign.SCRIPTS / "prepare_session.sh"):
                prepared.touch()
                env["CODEX_THREAD_ID"] = self.native.fixture.parent.current_session
                return subprocess.CompletedProcess(command, 0, str(self.session_dir) + "\n", "")
            return original(command, env, capture=capture, check=check)
        _ = self.enterContext(patch.object(assign, "call", side_effect=call))
        return prepared


class HookAssignmentFixture(AssignLauncherFixture):
    """Deliver native completion events after the launcher returns."""

    @override
    def launch(self, role: str = "implement", *options: str, paths: bool = True, session: bool = True,
               execution: AssignmentExecution = DEFAULT_ASSIGNMENT_EXECUTION) -> subprocess.CompletedProcess[str]:
        result = super().launch(role, *options, paths=paths, session=session, execution=execution)
        if result.returncode == 0 and self.native.agent.submitted:
            row = self.native.agent.row(self.native.agent.submitted[-1][0])
            _ = self.native.finish(row.name)
        return result


class FirstDispatchFixture(HookAssignmentFixture):
    def first_assignment(self, role: str = "implement", *options: str) -> None:
        _ = self.successful(role, *options)
