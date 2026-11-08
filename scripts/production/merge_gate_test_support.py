"""Independent scratch repositories and real runner jobs for merge-gate tests."""

from __future__ import annotations

from scripts.shutdown import remote

from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
import json
import contextlib
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


from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission, only_admitted
from scripts.lib import build_folders
from scripts.lint import memory_admit, sweep
from scripts.lint.memory_test_support import fake_memory_readings, write_config_fixture
from scripts.production import merge_gate, staff, targeted_test
from scripts.production.staff_test_support import fixture_member, isolated_staff_file
from scripts.production.cli_fixture import invoke_cli
from scripts.test_times.gate_record import GateRunRecord
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS, LauncherHangGuard


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("merge_gate.py")
# Fixture data, independent of the checkout's test folders.
FIXTURE_FOLDERS = ("lib", "hooks", "message", "notify", "direct", "fixture_a", "fixture_b", "fixture_c")
# Each folder holds test_first.py, and lib also holds test_second.py.


def install_test_runner(checkout: Path) -> Path:
    """Install the real runner and its descendant helper in a fixture checkout."""
    for relative in ("test_times/module_run.py", "lib/descendants.py"):
        destination = checkout / "scripts" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copyfile(SCRIPT.parents[1] / relative, destination)
    return checkout / "scripts/test_times/module_run.py"


TOOL_STUB = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

TEST_HANG_GUARD_SECS = __TEST_HANG_GUARD_SECS__
with open(Path(os.environ["MERGE_GATE_TEST_REPO"]) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
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
        print("scripts/fixture.py:1: error: fixture type error")
        print("1 error, 0 warnings, 0 notes")
        raise SystemExit(1)
    print("0 errors, 0 warnings, 0 notes")
    raise SystemExit(0)
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
                              capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.split()[0]
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
        import select
        self.assertTrue(select.select([sys.stdin], [], [], TEST_HANG_GUARD_SECS)[0], "module stdin did not close")
        self.assertEqual(sys.stdin.read(), "")
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_DFL)
        self.assertEqual(signal.getsignal(signal.SIGQUIT), signal.SIG_DFL)
        if os.environ.get("MERGE_GATE_TEST_ISOLATION") == "1":
            self.assertEqual(os.environ.get("PYTHONPATH"), str(Path.cwd()))
            self.assertNotIn(os.environ["MERGE_GATE_TEST_PYTHONPATH"], sys.path)
            self.assertNotIn(os.environ["MERGE_GATE_TEST_REPO"], sys.path)
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
            statuses = [subprocess.run(command, env=environment, check=False, timeout=TEST_HANG_GUARD_SECS).returncode for command in commands]
            (state / "tool-statuses.json").write_text(json.dumps(statuses))
        if (state / "concurrency-barrier").exists():
            (state / (str(os.getpgrp()) + ".started")).touch()
            deadline = time.monotonic() + TEST_HANG_GUARD_SECS
            while not (state / (str(os.getpgrp()) + ".released")).exists():
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
            (state / "module-held.tmp").write_text(str(os.getpgrp()))
            # Publish by rename: a reader must never see the file empty.
            os.replace(state / "module-held.tmp", state / "module-held")
            signal.alarm(TEST_HANG_GUARD_SECS)
            signal.pause()
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


class MergeGateFixture(unittest.TestCase):
    suite_mode: ClassVar[merge_gate.SuiteMode] = merge_gate.SuiteMode.MERGE_GATE

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
        self.suites: tuple[str, ...] = ()
        self.admission_started: bool = False

    admission: AdmissionFixture = cast(AdmissionFixture, cast(object, None))

    fixture_root: ClassVar[Path]

    fixture_snapshot: ClassVar[Path]

    fixture_seeds: ClassVar[dict[tuple[str, ...], GateFixtureSeed]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(dir=build_folders.tmpfs_temporary_parent(), prefix="merge-gate-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.fixture_snapshot = Path(directory.name) / "snapshot"
        cls.fixture_snapshot.mkdir()
        cls.fixture_seeds = {}

    @override
    def setUp(self) -> None:
        _ = self.enterContext(LauncherHangGuard(self.id()).protect())
        self.restore_fixture(suites_for_case(self))
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.admission = caller_admission(self, self.root, self.environment)
        self.admission_started = True

    def restore_fixture(self, folders: tuple[str, ...]) -> None:
        """Restore a clean private tree with only the test folders this case needs."""
        restart_admission = self.admission_started
        if restart_admission:
            self.admission.close()
        if self.fixture_root.exists():
            shutil.rmtree(self.fixture_root)
        self.suites = folders
        if folders not in self.fixture_seeds:
            self.fixture_root.mkdir()
            self.build_fixture(self.fixture_root)
            snapshot = self.fixture_snapshot / str(len(self.fixture_seeds))
            _ = shutil.copytree(self.fixture_root, snapshot)
            self.fixture_seeds[folders] = GateFixtureSeed(snapshot, self.base, self.checkpoint)
        else:
            # Git worktree metadata has absolute paths. Each copy must return
            # to the seed's original path, with its private origin and refs.
            _ = shutil.copytree(self.fixture_seeds[folders].snapshot, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        seed = self.fixture_seeds[folders]
        self.base, self.checkpoint = seed.base, seed.checkpoint
        if restart_admission:
            self.admission = caller_admission(self, self.root, self.environment)

    @property
    def module_count(self) -> int:
        return len(self.suites) + int("lib" in self.suites)

    def configure_gate(self, overrides: str) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_admission.conf", overrides)

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
            **os.environ, "HOME": str(home), "STAFF_FILE": str(home / "staff.json"), "XDG_STATE_HOME": str(home / "state"),
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
        readings = root / "memory"
        readings.mkdir(exist_ok=True)
        self.environment.update(fake_memory_readings(readings))
        self.environment.update({
            "BUILDLOG_DIR": str(root / "buildlog"),
            "BUILDLOG_JOBSERVER_FIFO": str(root / "jobserver"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "merge-gate",
            "NOTIFIER_SESSIONS_DIR": str(home / "sessions"),
        })
        # The seed is copied with copytree; create the FIFO only in restored cases.
        if self.origin.exists():
            os.mkfifo(self.environment["BUILDLOG_JOBSERVER_FIFO"])
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
            _ = os.write(descriptor, b"+" * 31)

    def jobserver_tokens(self) -> bytes:
        """Read the private pool without changing the available slots."""
        descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
        try:
            try:
                tokens = os.read(descriptor, 4096)
            except BlockingIOError:
                tokens = b""
            if tokens:
                self.assertEqual(os.write(descriptor, tokens), len(tokens))
            return tokens
        finally:
            os.close(descriptor)

    def supply_jobserver(self, tokens: bytes) -> None:
        descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
        try:
            try:
                _ = os.read(descriptor, 4096)
            except BlockingIOError:
                pass
            if tokens:
                self.assertEqual(os.write(descriptor, tokens), len(tokens))
        finally:
            os.close(descriptor)

    @property
    def admission_directory(self) -> Path:
        return Path(self.environment["BUILDLOG_DIR"]) / "admission"

    def gate_history(self) -> list[dict[str, object]]:
        path = self.admission_directory / "anon_peaks.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def occupy_memory_queue(self) -> wire.Admitted:
        request = self.admission.request(need=21 * memory_admit.GIB, step="build")
        with self.admission.loop_turn():
            return only_admitted(self.admission.server.handle_request(request))

    def assert_gate_released(self) -> None:
        self.assertEqual(self.admission.server.reservations, {})
        self.assertEqual(self.admission.server.waiters, {})
        for suffix in ("reservation", "waiting", "max", "wave"):
            self.assertEqual(list(self.admission_directory.glob("*." + suffix)), [], suffix)

    @contextlib.contextmanager
    def observing_admission(self, observe: Callable[[memory_admit.AdmissionWaitSnapshot], None]) -> Generator[None]:
        server = self.admission.server
        previous = server.reply_filter

        def observed(reply: wire.Reply) -> wire.Reply:
            if isinstance(reply, wire.Wait):
                observe(reply.snapshot)
            elif isinstance(reply, wire.Admitted):
                record = next(row for row in server.reservations.values() if row.reservation_id == reply.reservation)
                waiter = record.queue_waiter
                inputs = server.last_inputs
                result = server.last_result
                self.assertIsInstance(inputs, memory_admit.QueueInputs)
                self.assertIsInstance(result, memory_admit.PassResult)
                assert isinstance(inputs, memory_admit.QueueInputs)
                assert isinstance(result, memory_admit.PassResult)
                outcome = next(row for row in result.rows if row.owner == memory_admit.waiter_owner(waiter))
                peak = memory_admit.ExpectedPeak(reply.need, reply.source, waiter.get("need_runs", 0),
                    memory_admit.read_need_basis(waiter.get("need_basis")))
                available = inputs.host.available if isinstance(inputs.host, memory_admit.HostMemory) else 0
                policy = server.last_policy
                assert isinstance(policy, memory_admit.AdmissionPolicy)
                observe(memory_admit.AdmissionWaitSnapshot(outcome.decision, peak, tuple(inputs.running),
                    available, record.repo, record.step, policy, server.last_diagnostics))
            return previous(reply)

        server.reply_filter = observed
        try:
            yield
        finally:
            server.reply_filter = previous

    def build_fixture(self, root: Path) -> None:
        self.bind_fixture(root)
        self.bin.mkdir()
        self.state.mkdir()
        (self.root / "home").mkdir()
        # A production with no Change review line: a gate always names one, and these write no rows.
        self.quiet_production = self.root / "quiet-production.md"
        self.write(self.quiet_production, "# quiet\n")
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            _ = fixture_member("showrunner", name="quiet", doc=staff.LiveDoc(remote.local_machine(), self.quiet_production))
        _ = self.git(self.root, "init", "--bare", str(self.origin))
        _ = self.git(self.root, "init", "-b", "main", str(self.checkout))
        _ = self.git(self.checkout, "config", "user.name", "Merge Gate Fixture")
        _ = self.git(self.checkout, "config", "user.email", "gate@example.invalid")
        _ = self.git(self.checkout, "remote", "add", "origin", str(self.origin))
        self.write(self.checkout / "settings.json", '{"fixture": true}\n')
        self.write(self.checkout / "subject.txt", "base\n")
        self.write(self.checkout / "Cargo.toml", "[workspace]\n")
        self.write(self.checkout / "scripts" / "production" / "README.md", "Type-check fixture.\n")
        _ = install_test_runner(self.checkout)
        shim = self.checkout / "scripts/lib/py"
        shim.parent.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(SCRIPT.parents[1] / "lib/py", shim)
        for suite in self.suites:
            self.write(self.checkout / "scripts" / suite / "test_first.py", SUITE_MODULE)
        if "lib" in self.suites:
            self.write(self.checkout / "scripts" / "lib" / "test_second.py", SUITE_MODULE)
        self.write(self.checkout / ".gitignore", "__pycache__/\n")
        self.base = self.commit(self.checkout, "base")
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

    def gate_run_records(self) -> list[GateRunRecord]:
        paths = sorted(Path(self.environment["BUILDLOG_DIR"]).glob("*/*.jsonl"))
        return [cast(GateRunRecord, cast(object, row))
                for row in sweep.build_log_rows(paths) if row.get("kind") == "gate_run"]

    def prepare_affected_subject(self) -> tuple[Path, str]:
        subject = self.checkout / "scripts/hooks/affected_subject.py"
        self.write(subject, "VALUE = 1\n")
        self.write(self.checkout / "scripts/hooks/test_first.py", "from scripts.hooks import affected_subject\n" + SUITE_MODULE)
        return subject, self.commit(self.checkout, "dependency fixture")

    def run_gate(self, *extra: str, checkpoint: str = "", previous: str = "",
                 ignored_signals: bool = False, callers_production: bool = False,
                 own_process: bool = False, preamble: str = "",
                 type_dirs: Sequence[str] = ("scripts/production",)) -> subprocess.CompletedProcess[str]:
        arguments = ["--repo", str(self.checkout),
             "--merge-branch", "integration", "--unit-branch", "unit-phase",
             "--checkpoint", checkpoint or self.checkpoint,
             "--previous-checkpoint", previous or self.base,
             "--unit", "fixture", "--phase", "phase-2",
             "--body", "Fixture checkpoint gate.",
             *(["--type-dirs", *type_dirs] if type_dirs else []),
             "--main-repo", str(self.main),
             "--ssh", str(self.bin / "ssh"),
             *([] if callers_production or "--production" in extra else ["--production", "quiet"]),
             *extra]
        if self.suite_mode is merge_gate.SuiteMode.TARGETED_TEST:
            arguments = ["--repo", str(self.checkout),
                         *(["--type-dirs", *type_dirs] if type_dirs else []), *extra]
        if own_process or ignored_signals:
            return subprocess.run(
                [*self.gate_command(ignored_signals=ignored_signals, preamble=preamble), *arguments], cwd=self.root,
                env=self.environment, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        from scripts.production import showrunners

        # Restore the registry's HOME-derived paths after each call.
        home = Path(self.environment["HOME"])
        with contextlib.ExitStack() as scope:
            for name, value in {
                "NOTIFIER_STATE_DIR": Path(self.environment["NOTIFIER_STATE_DIR"]),
                "SHOWRUNNER_STATE_DIR": home / ".local/state/showrunner",
            }.items():
                _ = scope.enter_context(mock.patch.object(showrunners, name, value))
            entry = targeted_test if self.suite_mode is merge_gate.SuiteMode.TARGETED_TEST else merge_gate
            return invoke_cli(entry, lambda: entry.main(arguments), arguments,
                              self.environment, self.root)

    def gate_command(self, *, ignored_signals: bool = False, preamble: str = "") -> list[str]:
        source = "import sys, signal; "
        if ignored_signals:
            source += ("[signal.signal(sig, signal.SIG_IGN) for sig in "
                       "(signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)]; ")
        source += ("from scripts.production import merge_gate; \n"
                   + ("from scripts.production import targeted_test as entry\n"
                      if self.suite_mode is merge_gate.SuiteMode.TARGETED_TEST else "entry = merge_gate\n")
                   + preamble + "\n"
                   + "raise SystemExit(entry.main(sys.argv[1:]))")
        return [str(SCRIPT.parents[1] / "lib/py"), "-c", source]

    def run_promote_only(self, *extra: str, explicit_checkout: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT.resolve().parents[1] / "lib" / "py"), str(SCRIPT),
             *(["--repo", str(self.checkout)] if explicit_checkout else []), "--merge-branch", "integration",
             "--promote-only", "--main-repo", str(self.main),
             "--ssh", str(self.bin / "ssh"),
             *([] if "--production" in extra else ["--production", "quiet"]), *extra],
            cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
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

    def write_nightly_red_marker(self) -> tuple[Path, str]:
        marker = Path(self.environment["MERGE_GATE_STATE_DIR"]) / "full-required"
        notice = json.dumps({"tree": "red-tree", "at": "2026-10-29T00:00:00+00:00"})
        self.write(marker, notice)
        return marker, notice

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
        review = Path(self.environment["HOME"]) / "rust/hanadocs/change-review/change-review - fixture-showrunner.md"
        self.write(review, "## To review\n\n| # | Change | Merged | On the Mac build | How to check |\n"
                   + "| --- | --- | --- | --- | --- |\n| C7 | old | 2026-10-14 | yes | - |\n")
        doc = self.root / "production.md"
        self.write(doc, f"# p\n\n## Production Context\n\n- **Change review:** `{review}`\n"
                   + f"- **Repository:** `{self.checkout}`\n- **Showrunner checkout:** `{self.checkout}`\n"
                   + "- **Merge branch:** `integration`\n")
        with isolated_staff_file(Path(self.environment["STAFF_FILE"]), self.environment):
            _ = fixture_member("showrunner", name="fixture-showrunner", doc=staff.LiveDoc("natedev", doc))
        return review, doc

    def publish_for_promotion(self) -> merge_gate.GateArguments:
        _ = self.git(self.checkout, "merge", "--ff-only", "unit-phase")
        _ = self.git(self.checkout, "push", "origin", "integration")
        return merge_gate.GateArguments(
            main_repo=str(self.main), merge_branch="integration",
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
