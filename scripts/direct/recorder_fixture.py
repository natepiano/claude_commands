"""Run the real recorder CLI against disposable state, including its lock."""

from __future__ import annotations

from scripts.lib.py_floor_test_support import seed_interpreter_cache

from scripts.lib.process_group import send_process_group
from scripts.lib import test_hang_guard

from collections.abc import Mapping, Sequence
from pathlib import Path
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import ClassVar, cast, override
from unittest.mock import patch

# Initialize dependencies before invoke_cli swaps HOME. The recorder currently
# imports user_zone eagerly; its production integrations remain real children.
from scripts.direct import progress_history
from scripts.lib import models_registry, user_zone
from scripts.lib.test_models_registry import write_registry
from scripts.production.cli_fixture import invoke_cli
from scripts.production import session_staff, staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, fixture_worker, isolated_staff_file


def record_wait_workers(root: Path, session: Path, environment: dict[str, str],
                        workers: Mapping[str, str]) -> None:
    """Publish private staff references before the recorder attributes a wait."""
    with isolated_staff_file(root / "staff.json", environment):
        owner = fixture_member("showrunner", name="wait-owner")
        parent = fixture_member("unit", name="wait", parent=owner.id,
                                runtime_id=fixture_runtime_id("fixture-director"),
                                worktree=staff.PresentWorktree("natedev", root))
        entries = {name: {"staff_id": fixture_worker(name, parent.id, runtime_id=fixture_runtime_id(identity)).id}
                  for name, identity in workers.items()}
    session.mkdir(parents=True, exist_ok=True)
    for name, entry in entries.items():
        _ = (session / f"impl_worker_{name}.json").write_text(json.dumps({"slot": name, **entry}))


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


class LauncherFixture(unittest.TestCase):
    """Prepare one seed per launcher class and give every test a private copy."""
    fixture_seed: ClassVar[Path]
    root: Path = Path()
    history_dir: Path = Path()
    working_dir: Path = Path()
    config_file: Path = Path()
    prompt_file: Path = Path()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.fixture_seed = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        seed = cls("runTest")
        seed.root = cls.fixture_seed
        seed.prepare_fixture()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Private files and Git objects/index/refs: mutations never reach another test.
        _ = shutil.copytree(self.fixture_seed, self.root, dirs_exist_ok=True)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "project"
        self.config_file = self.root / "direct.conf"

    def prepare_fixture(self) -> None:
        raise NotImplementedError("the launcher fixture must prepare its seed")

    def prepare_project(self, branch: str, prompt_name: str, prompt: str,
                        configuration: Sequence[str], registry: str) -> None:
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "project"
        self.working_dir.mkdir()
        _ = subprocess.run(["git", "init", "-b", branch], cwd=self.working_dir,
                           check=True, capture_output=True, text=True)
        self.config_file = self.root / "direct.conf"
        _ = self.config_file.write_text("\n".join(configuration), encoding="utf-8")
        registry_file = self.root / ".claude/config/models.conf"
        registry_file.parent.mkdir(parents=True)
        _ = write_registry(registry_file, base_text=registry)
        self.prompt_file = self.root / prompt_name
        _ = self.prompt_file.write_text(prompt, encoding="utf-8")

    def recorder(self, *arguments: str) -> None:
        environment = self.environment()
        environment["UNIT_PASS_OWNER"] = "launcher"
        result = invoke_recorder(arguments, environment, Path.cwd())
        self.assertEqual(result.returncode, 0, result.stderr)

    def environment(self) -> dict[str, str]:
        raise NotImplementedError("the launcher fixture must supply its environment")

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


def prepare_launcher_runtime(root: Path) -> tuple[Path, Path, Path]:
    """Install shared launcher dependencies once in the disposable script tree."""
    source = Path(__file__).resolve().parents[1]
    direct, agents, lib = (root / "scripts" / name for name in ("direct", "agents", "lib"))
    for directory in (direct, agents, lib, root / "scripts/production"):
        directory.mkdir(parents=True)
    for name in ("py", "process_group.py", "atomic_file.py", "user_zone.py", "unit_runs.py", "models_registry.py", "process_failure.py",
                 "codex_models_cache.py", "build_folders.py"):
        _ = shutil.copy2(source / "lib" / name, lib / name)
    home_lib = root / ".claude/scripts/lib"
    home_lib.mkdir(parents=True, exist_ok=True)
    for name in ("py", "process_group.py"):
        _ = shutil.copy2(source / "lib" / name, home_lib / name)
    _ = shutil.copy2(source / "production/production_doc.py", root / "scripts/production/production_doc.py")
    for name in ("models_config.sh", "heartbeat.sh", "heartbeat_watch.sh", "worker_lifecycle.py"):
        _ = shutil.copy2(source / "agents" / name, agents / name)
    for name in ("assign.sh", "assign.py", "assign_claude.py", "claude_worker.py", "assign_gate.py", "workers.py", "end_session.sh", "remove_run_scratch.py", "worker_name.sh", "worker_state.py", "progress_history.py", "plan_history.py",
                 "phase_reservation.py", "recorder_state.py", "findings.py", "board.sh"):
        _ = shutil.copy2(source / "direct" / name, direct / name)
    return direct, agents, lib


def configure_launcher_fixture(launcher: Path) -> None:
    """Keep legacy recorder calls and bounded fixture heartbeats in one place."""
    recorder = launcher.with_name("progress_history.py")
    _ = recorder.write_text(recorder.read_text().replace(
        "from __future__ import annotations\n",
        "from __future__ import annotations\n" + LEGACY_RECORDER_ARGUMENTS, 1,
    ))
    _ = launcher.write_text(launcher.read_text(encoding="utf-8").replace(
        "HEARTBEAT_INTERVAL_SECS=60", "HEARTBEAT_INTERVAL_SECS=1"), encoding="utf-8")


def launcher_environment(root: Path) -> dict[str, str]:
    """Isolate registry writes, Python discovery and worker identity for launchers."""
    environment = {name: value for name, value in os.environ.items() if not name.startswith("HERDR_")}
    environment.update(MODELS_CONFIG_PUBLISH="0", HOME=str(root), XDG_CACHE_HOME=str(root / "cache"),
                       UNIT_WORKER_POLL_SECS="0.01", UNIT_SYNC_WRITES="0",
                       TEST_LEGACY_LAUNCH="1", MODELS_CONFIG_FILE=str(root / ".claude/config/models.conf"),
                       UNIT_HISTORY_DIR=str(root / "history"), CONF_DIR=str(root),
                       TZ="UTC", USER_ZONE="UTC")
    seed_interpreter_cache(root / "cache")
    for name in ("CLAUDE_PYTHON", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID"):
        _ = environment.pop(name, None)
    return environment


def communicate_launcher(process: subprocess.Popen[str], timeout: float = test_hang_guard.TEST_HANG_GUARD_SECS) -> tuple[str, str]:
    """Reap a fixture's isolated launcher group before reporting a hang."""
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            send_process_group(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _ = process.communicate()
        raise


def invoke_recorder(arguments: Sequence[str], environment: Mapping[str, str],
                    cwd: Path, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    home = Path(environment["HOME"])

    def main() -> int:
        if arguments and arguments[0] == "start-pass" and "--worker-name" in arguments and "--worker-id" not in arguments:
            session = Path(arguments[arguments.index("--session-dir") + 1])
            name = arguments[arguments.index("--worker-name") + 1]
            state = cast(dict[str, object], json.loads((session / "progress_history_state.json").read_text()))
            matches = [row for row in session_staff.run_workers(state, staff.local_snapshot()) if row.name == name]
            if len(matches) == 1:
                sys.argv.extend(("--worker-id", matches[0].id))
        progress_history.main()
        return 0

    progress_history._config_digest.cache_clear()  # pyright: ignore[reportPrivateUsage]
    try:
        with (
            patch.dict(os.environ, {"CONF_DIR": environment.get("CONF_DIR", str(home / ".claude/config"))}),
            patch.object(models_registry, "DEFAULT_PATH", home / ".claude/config/models.conf"),
            patch.object(user_zone, "CONF", Path(environment.get("USER_ZONE_CONF") or
                                               str(Path(user_zone.__file__).parents[2] / "config/user_zone.conf"))),
        ):
            result = invoke_cli(progress_history, main, list(arguments), environment, cwd)
    finally:
        progress_history._config_digest.cache_clear()  # pyright: ignore[reportPrivateUsage]
    if check:
        result.check_returncode()
    return result
