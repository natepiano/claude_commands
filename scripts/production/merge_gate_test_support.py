"""Independent scratch repositories and real runner jobs for merge-gate tests."""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
import json
import contextlib
from importlib import import_module
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import ClassVar, ParamSpec, TypedDict, cast, override
import unittest
from unittest import mock
from scripts.production import merge_gate
from scripts.production.cli_fixture import invoke_cli
from scripts.direct.wait_for_test_state import wait_until


# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("merge_gate.py")
# Fixture data, independent of the checkout's test folders.
FIXTURE_FOLDERS = ("lib", "hooks", "message", "notify", "direct", "fixture_a", "fixture_b", "fixture_c")
# Each folder holds test_first.py, and lib also holds test_second.py.

TOOL_STUB = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

TEST_HANG_GUARD_SECS = __TEST_HANG_GUARD_SECS__
sys.path.insert(0, os.environ["MERGE_GATE_TEST_REPO"])
from scripts.direct.wait_for_test_state import wait_until
state = Path(os.environ["MERGE_GATE_TEST_STATE"])
name = Path(sys.argv[0]).name
with (state / "tools.jsonl").open("a") as output:
    output.write(json.dumps({"tool": name, "args": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid()}) + "\\n")
if name == "systemd-run":
    if (state / "scope-fails").exists():
        raise SystemExit(1)
    command = sys.argv[sys.argv.index("--") + 1:]
    os.execvp(command[0], command)
if name == "basedpyright":
    (state / "types-ran").write_text("ran")
    if (state / "types-barrier").exists():
        wait_until((state / "modules.jsonl").exists, state, timeout=TEST_HANG_GUARD_SECS)
    if (state / "types-hold").exists():
        # The parent publishes only after measuring this type job's real RSS.
        wait_until((state / "types-sampled").exists, state, timeout=TEST_HANG_GUARD_SECS)
    if (state / "types-red").exists():
        print("fixture type error")
        print("1 error, 0 warnings, 0 notes")
        raise SystemExit(1)
    print("0 errors, 0 warnings, 0 notes")
    raise SystemExit(3)
if name == "uv":
    if (state / "uv-fails").exists():
        print("fixture: no network", file=sys.stderr)
        raise SystemExit(2)
    if sys.argv[1] == "venv":
        python = Path(sys.argv[-1]) / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("")
    raise SystemExit(0)
if name == "ssh":
    answer = (state / "mac-answer").read_text() if (state / "mac-answer").exists() else "ff_rc=0\\nfixture-head\\n"
    print(answer, end="")
    raise SystemExit(0)
if name == "suite-python":
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
if name == "style_sync.py":
    import subprocess
    args = sys.argv[1:]
    repo = args[args.index("--repo-dir") + 1]
    branch = args[args.index("--branch") + 1]
    observed = subprocess.run(["git", "-C", repo, "ls-remote", "origin", "refs/heads/" + branch],
                              capture_output=True, text=True, check=True).stdout.split()[0]
    (state / "notified-tip").write_text(observed)
    raise SystemExit(1 if (state / "notify-fails").exists() else 0)
raise SystemExit("unexpected tool: " + name)
'''.replace("__TEST_HANG_GUARD_SECS__", str(TEST_HANG_GUARD_SECS))

SUITE_MODULE = '''import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
import unittest

TEST_HANG_GUARD_SECS = __TEST_HANG_GUARD_SECS__
state = Path(os.environ["MERGE_GATE_TEST_STATE"])
module = Path(__file__)
suite = module.relative_to(Path.cwd() / "scripts").parts[0]
module_name = module.relative_to(Path.cwd() / "scripts" / suite).as_posix()

def record(event):
    with (state / "modules.jsonl").open("a") as output:
        fcntl.flock(output, fcntl.LOCK_EX)
        output.write(json.dumps(event) + "\\n")
        output.flush()

class FixtureTest(unittest.TestCase):
    def test_gate_fixture(self):
        tmp = Path(os.environ["TMPDIR"])
        self.assertTrue(tmp.is_dir())
        self.assertTrue(str(tmp).startswith("/dev/shm/"), tmp)
        buildlog = Path(os.environ["BUILDLOG_DIR"])
        self.assertTrue(buildlog.is_dir())
        self.assertTrue(buildlog.is_relative_to(tmp), buildlog)
        (buildlog / "fixture-log").write_text("private log")
        with (state / "buildlogs.jsonl").open("a") as output:
            fcntl.flock(output, fcntl.LOCK_EX)
            output.write(json.dumps({"tmpdir": str(tmp), "buildlog": str(buildlog)}) + "\\n")
        self.assertEqual(sys.stdin.read(), "")
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)
        self.assertEqual(signal.getsignal(signal.SIGQUIT), signal.SIG_DFL)
        if os.environ.get("MERGE_GATE_TEST_ISOLATION") == "1":
            self.assertEqual(os.environ.get("PYTHONPATH"), os.environ.get("MERGE_GATE_TEST_PYTHONPATH"))
        record({"suite": suite, "module": module_name, "pid": os.getpid(),
                "event": "start", "tmpdir": str(tmp)})
        death = state / ("signal-" + suite)
        if death.exists():
            if death.read_text() == "once":
                death.unlink()
            os.kill(os.getpid(), signal.SIGTERM)
        if suite == "hooks" and (state / "tool-commands.json").exists():
            import subprocess
            commands = json.loads((state / "tool-commands.json").read_text())
            environment = dict(os.environ)
            if (state / "own-tool-stubs").exists():
                tools = tmp / "own-tools"
                tools.mkdir()
                for command in commands:
                    stub = tools / command[0]
                    stub.write_text("#!/bin/sh\\nexit 0\\n")
                    stub.chmod(0o755)
                environment["PATH"] = str(tools) + os.pathsep + environment["PATH"]
            statuses = [subprocess.run(command, env=environment, check=False).returncode for command in commands]
            (state / "tool-statuses.json").write_text(json.dumps(statuses))
        if (state / "concurrency-barrier").exists():
            (state / (str(os.getpid()) + ".started")).touch()
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            while not (state / (str(os.getpid()) + ".released")).exists():
                if time.monotonic() >= deadline:
                    self.fail("runner never released the module")
                time.sleep(0.01)
        attempt = state / "attempts" / suite / module_name
        attempt.parent.mkdir(parents=True, exist_ok=True)
        first_run = not attempt.exists()
        attempt.write_text("ran")
        if (state / ("fail-first-" + suite)).exists() and first_run:
            self.fail("fixture first run in " + suite + "/" + module.name)
        if not first_run:
            print("fixture rerun in " + suite + "/" + module.name)
        if (state / "leak-child").exists() and suite == "hooks":
            import subprocess
            child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (state / "child-pid.tmp").write_text(str(child.pid))
            # Publish by rename: a reader must never see the file empty.
            os.replace(state / "child-pid.tmp", state / "child-pid")
        if (state / "modify-tracked").exists() and suite == "hooks":
            Path("unit.txt").write_text("gate debris\\n")
            self.fail("fixture edited a tracked checkpoint file")
        if (state / "hold-module").exists() and suite == "hooks":
            (state / "module-held.tmp").write_text(str(os.getpid()))
            # Publish by rename: a reader must never see the file empty.
            os.replace(state / "module-held.tmp", state / "module-held")
            signal.pause()
        if (state / "grow-memory").exists() and suite == "hooks":
            # Grows until the runner kills it; past 2 GiB the cap failed to stop it.
            blocks = []
            while len(blocks) < 256:
                blocks.append(b"x" * (8 << 20))
                time.sleep(0.02)
            self.fail("fixture grew to 2 GiB and was never killed")
        if (state / "barrier").exists() and suite == "lib":
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            while True:
                with (state / "modules.jsonl").open() as source:
                    starts = [json.loads(line) for line in source if line.strip()]
                if len([row for row in starts if row["suite"] == "lib" and row["event"] == "start"]) == 2:
                    break
                if time.monotonic() >= deadline:
                    self.fail("module processes never overlapped")
                time.sleep(0.01)
        if (state / ("red-" + suite)).exists():
            self.fail("fixture failure in " + suite + "/" + module.name)
        record({"suite": suite, "module": module_name, "pid": os.getpid(),
                "event": "finish", "tmpdir": str(tmp)})
'''.replace("__TEST_HANG_GUARD_SECS__", str(TEST_HANG_GUARD_SECS))


class ToolCall(TypedDict):
    tool: str
    args: list[str]
    cwd: str
    pid: int


class ModuleEvent(TypedDict):
    suite: str
    module: str
    pid: int
    event: str
    tmpdir: str


GateTestParameters = ParamSpec("GateTestParameters")


def gate_suites(*suites: str) -> Callable[[Callable[GateTestParameters, None]], Callable[GateTestParameters, None]]:
    """Declare the discovery subjects expected independently of the runner."""
    def decorate(test: Callable[GateTestParameters, None]) -> Callable[GateTestParameters, None]:
        setattr(test, "gate_test_suites", suites)
        return test
    return decorate


def suites_for_case(case: unittest.TestCase) -> tuple[str, ...]:
    method: object = getattr(type(case), case._testMethodName, None)
    return cast(tuple[str, ...], getattr(method, "gate_test_suites", ("hooks",)))


def wait_for_file(path: Path) -> None:
    wait_until(path.exists, path.parent, timeout=TEST_HANG_GUARD_SECS)


@dataclass(frozen=True)
class GateFixtureSeed:
    snapshot: Path
    base: str
    checkpoint: str
    settings_blob: str


class MergeGateFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.origin: Path = Path()
        self.checkout: Path = Path()
        self.unit: Path = Path()
        self.main: Path = Path()
        self.bin: Path = Path()
        self.state: Path = Path()
        self.quiet_production: Path = Path()
        self.environment: dict[str, str] = {}
        self.base: str = ""
        self.checkpoint: str = ""
        self.settings_blob: str = ""
        self.suites: tuple[str, ...] = ()

    fixture_root: ClassVar[Path]

    fixture_snapshot: ClassVar[Path]

    fixture_seeds: ClassVar[dict[tuple[str, ...], GateFixtureSeed]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="merge-gate-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.fixture_snapshot = Path(directory.name) / "snapshot"
        cls.fixture_snapshot.mkdir()
        cls.fixture_seeds = {}

    @override
    def setUp(self) -> None:
        self.restore_fixture(suites_for_case(self))
        self.addCleanup(shutil.rmtree, self.fixture_root)

    def restore_fixture(self, folders: tuple[str, ...]) -> None:
        """Restore a clean private tree with only the test folders this case needs."""
        if self.fixture_root.exists():
            shutil.rmtree(self.fixture_root)
        self.suites = folders
        if folders not in self.fixture_seeds:
            self.fixture_root.mkdir()
            self.build_fixture(self.fixture_root)
            snapshot = self.fixture_snapshot / str(len(self.fixture_seeds))
            _ = shutil.copytree(self.fixture_root, snapshot)
            self.fixture_seeds[folders] = GateFixtureSeed(snapshot, self.base, self.checkpoint, self.settings_blob)
        else:
            # Git worktree metadata has absolute paths. Each copy must return
            # to the seed's original path, with its private origin and refs.
            _ = shutil.copytree(self.fixture_seeds[folders].snapshot, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        seed = self.fixture_seeds[folders]
        self.base, self.checkpoint, self.settings_blob = seed.base, seed.checkpoint, seed.settings_blob

    @property
    def module_count(self) -> int:
        return len(self.suites) + int("lib" in self.suites)

    def record_module_peaks(self, peaks: dict[str, float]) -> None:
        merge_gate.save_measures(self.state / "runner" / "module_peak_mib.json", peaks)

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "merge"
        self.unit = self.root / "unit"
        self.main = self.root / "main"
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.quiet_production = self.root / "quiet-production.md"
        home = self.root / "home"
        self.environment = {
            **os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
            "CODEX_HOME": str(home / "codex"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "MERGE_GATE_TEST_STATE": str(self.state),
            "MERGE_GATE_TEST_REPO": str(SCRIPT.parents[2]),
            "MERGE_GATE_PYTHON": str(self.bin / "suite-python"),
            "MERGE_GATE_BASEDPYRIGHT": str(self.bin / "basedpyright"),
            "MERGE_GATE_UV": str(self.bin / "uv"),
            "MERGE_GATE_STATE_DIR": str(self.state / "runner"),
            "MERGE_GATE_PASSED_DIR": str(self.state / "passes"),
            "MERGE_GATE_STYLE_SYNC": str(self.bin / "style_sync.py"),
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
            # The calling showrunner is found from these update timers, never the live ones.
            "NOTIFIER_STATE_DIR": str(home / "notifier"), "CLAUDE_CODE_SESSION_ID": "fixture-showrunner",
        }
        self.environment["MERGE_GATE_TEST_PYTHONPATH"] = self.environment.get("PYTHONPATH", "")

    def build_fixture(self, root: Path) -> None:
        self.bind_fixture(root)
        self.bin.mkdir()
        self.state.mkdir()
        (self.root / "home").mkdir()
        # A production with no Change review line: a gate always names one, and these write no rows.
        self.quiet_production = self.root / "quiet-production.md"
        self.write(self.quiet_production, "# quiet\n")
        _ = self.git(self.root, "init", "--bare", str(self.origin))
        _ = self.git(self.root, "init", "-b", "main", str(self.checkout))
        _ = self.git(self.checkout, "config", "user.name", "Merge Gate Fixture")
        _ = self.git(self.checkout, "config", "user.email", "gate@example.invalid")
        _ = self.git(self.checkout, "remote", "add", "origin", str(self.origin))
        self.write(self.checkout / "settings.json", '{"fixture": true}\n')
        self.write(self.checkout / "subject.txt", "base\n")
        self.write(self.checkout / "scripts" / "production" / "README.md", "Type-check fixture.\n")
        for suite in self.suites:
            self.write(self.checkout / "scripts" / suite / "test_first.py", SUITE_MODULE)
        if "lib" in self.suites:
            self.write(self.checkout / "scripts" / "lib" / "test_second.py", SUITE_MODULE)
        self.write(self.checkout / ".gitignore", "__pycache__/\n")
        self.base = self.commit(self.checkout, "base")
        self.settings_blob = self.git(self.checkout, "rev-parse", "HEAD:settings.json")
        _ = self.git(self.checkout, "push", "-u", "origin", "main")
        _ = self.git(self.checkout, "checkout", "-b", "integration")
        _ = self.git(self.checkout, "push", "-u", "origin", "integration")
        _ = self.git(self.checkout, "worktree", "add", "-b", "unit-phase", str(self.unit))
        _ = self.git(self.checkout, "worktree", "add", str(self.main), "main")
        self.write(self.unit / "unit.txt", "checkpoint\n")
        self.checkpoint = self.commit(self.unit, "unit checkpoint")
        _ = self.git(self.unit, "push", "-u", "origin", "unit-phase")
        for name in ("basedpyright", "uv", "ssh", "suite-python", "style_sync.py", "systemd-run"):
            self.write(self.bin / name, TOOL_STUB.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
            (self.bin / name).chmod(0o755)

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def assert_process_stopped(self, pid: int) -> None:
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while True:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":
                return
            if time.monotonic() >= deadline:
                self.fail(f"gate left running child {pid}")
            time.sleep(0.01)

    def git(self, checkout: Path, *arguments: str) -> str:
        # Every caller supplies one of this fixture's disposable paths explicitly.
        self.assertTrue(checkout.is_relative_to(self.root))
        result = subprocess.run(["git", "-C", str(checkout), *arguments],
                                env=self.environment, capture_output=True, text=True,
                                check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def commit(self, checkout: Path, subject: str) -> str:
        _ = self.git(checkout, "add", ".")
        _ = self.git(checkout, "commit", "-m", subject)
        return self.git(checkout, "rev-parse", "HEAD")

    def run_gate(self, *extra: str, checkpoint: str = "", previous: str = "",
                 ignored_signals: bool = False, callers_production: bool = False,
                 own_process: bool = False) -> subprocess.CompletedProcess[str]:
        arguments = ["--repo", str(self.checkout),
             "--merge-branch", "integration", "--unit-branch", "unit-phase",
             "--checkpoint", checkpoint or self.checkpoint,
             "--previous-checkpoint", previous or self.base,
             "--unit", "fixture-unit", "--phase", "phase-2",
             "--body", "Fixture checkpoint gate.", "--type-dirs", "scripts/production",
             "--settings-blob", self.settings_blob, "--main-repo", str(self.main),
             "--ssh", str(self.bin / "ssh"),
             *([] if callers_production or "--production" in extra else ["--production", str(self.quiet_production)]),
             *extra]
        # The gate measures itself and descendants; cap assertions need their own gate process.
        if own_process or ignored_signals:
            return subprocess.run(
                [*self.gate_command(ignored_signals=ignored_signals), *arguments], cwd=self.root,
                env=self.environment, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        from scripts.production import showrunners

        # change_review also imports this sibling by its script-local name.
        # Restore both import identities' HOME-derived paths after each call.
        home = Path(self.environment["HOME"])
        with contextlib.ExitStack() as scope:
            for module in (showrunners, import_module("showrunners")):
                for name, value in {
                    "NOTIFIER_STATE_DIR": Path(self.environment["NOTIFIER_STATE_DIR"]),
                    "SESSIONS_DIR": home / ".claude/sessions",
                    "SHOWRUNNER_STATE_DIR": home / ".local/state/showrunner",
                }.items():
                    _ = scope.enter_context(mock.patch.object(module, name, value))
            return invoke_cli(merge_gate, lambda: merge_gate.main(arguments), arguments,
                              self.environment, self.root)

    def gate_command(self, *, ignored_signals: bool = False) -> list[str]:
        source = "import sys, signal; "
        if ignored_signals:
            source += ("[signal.signal(sig, signal.SIG_IGN) for sig in "
                       "(signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)]; ")
        source += (f"sys.path.insert(0, {str(SCRIPT.parents[2])!r}); "
                   "from scripts.production import merge_gate; "
                   "raise SystemExit(merge_gate.main(sys.argv[1:]))")
        return [sys.executable, "-c", source]

    def run_promote_only(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.checkout), "--merge-branch", "integration",
             "--promote-only", "--settings-blob", self.settings_blob, "--main-repo", str(self.main),
             "--ssh", str(self.bin / "ssh")], cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def calls(self) -> list[ToolCall]:
        path = self.state / "tools.jsonl"
        return [cast(ToolCall, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def module_events(self) -> list[ModuleEvent]:
        path = self.state / "modules.jsonl"
        return [cast(ModuleEvent, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def pass_records(self) -> list[dict[str, object]]:
        path = Path(self.environment["MERGE_GATE_PASSED_DIR"]) / "passed-trees.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def clear_gate_events(self) -> None:
        for name in ("modules.jsonl", "tools.jsonl", "types-ran"):
            (self.state / name).unlink(missing_ok=True)

    def assert_all_modules_started(self) -> None:
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [(suite, "test_first.py") for suite in self.suites]
                              + ([("lib", "test_second.py")] if "lib" in self.suites else []))

    def assert_not_pushed(self) -> None:
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.base)

    def assert_refused_before_gate(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.module_events(), [])

    def checkpoint_commit(self, message: str) -> str:
        """A checkpoint(...) commit on the unit branch, dated after the Change-Review rule landed."""
        self.write(self.unit / "visible.txt", message)
        _ = self.git(self.unit, "add", ".")
        self.environment["GIT_COMMITTER_DATE"] = "2026-10-15T12:00:00-04:00"
        try:
            _ = self.git(self.unit, "commit", "-m", message)
        finally:
            del self.environment["GIT_COMMITTER_DATE"]
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        return self.git(self.unit, "rev-parse", "HEAD")

    def review_list(self) -> tuple[Path, Path]:
        """A change-review list holding C7, and the production doc that names it."""
        review = self.root / "change-review.md"
        self.write(review, "## To review\n\n| # | Change | Merged | On the Mac build | How to check |\n"
                   + "| --- | --- | --- | --- | --- |\n| C7 | old | 2026-10-14 | yes | - |\n")
        doc = self.root / "production.md"
        self.write(doc, f"# p\n\n## Production Context\n\n- **Change review:** `{review}`\n")
        return review, doc

    def publish_for_promotion(self) -> merge_gate.GateArguments:
        _ = self.git(self.checkout, "merge", "--ff-only", "unit-phase")
        _ = self.git(self.checkout, "push", "origin", "integration")
        return merge_gate.GateArguments(
            main_repo=str(self.main), merge_branch="integration", settings_blob=self.settings_blob,
            ssh=str(self.bin / "ssh"), mac_repo="~/fixture", mac_host="fixture")

    @contextlib.contextmanager
    def release_lock_held_by_child(self) -> Generator[subprocess.Popen[str], None, None]:
        lock = Path(self.environment["BUILD_HOLD_RELEASE_DIR"]) / "release.lock"
        self.write(lock, "")
        ready = self.state / "release-lock-held"
        source = """import fcntl, signal, sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_DFL)
with Path(sys.argv[1]).open("r+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    Path(sys.argv[2]).touch()
    sys.stdin.readline()
    fcntl.flock(lock, fcntl.LOCK_UN)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", source, str(lock), str(ready)], env=self.environment,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            wait_for_file(ready)
            yield process
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
