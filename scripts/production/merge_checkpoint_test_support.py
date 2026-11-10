"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

from scripts.production import checkpoint_gate

from scripts.agents import remote

from scripts.production.fake_unit_host import labeled as labeled_host

from scripts.lint.memory_test_support import config_environment
from collections.abc import Generator, Mapping, Sequence
from contextlib import closing, contextmanager
import fcntl
import json
import os
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock
from pathlib import Path
from typing import IO, ClassVar, NamedTuple, cast, override

from scripts.lib import process_group
from scripts.lib.git_test_support import write_fixture_git_config

from scripts.production import merge_checkpoint
from scripts.production import change_review
from scripts.direct import progress_history
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.production.cli_fixture import invoke_cli, process_boundary_for_case
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import fake_unit_host, staff
from scripts.message import sessions
from scripts.herdr.herdr import NATIVE_ID_MARKS
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, isolated_staff_file, production_membership, unit_membership


def invoke_checkpoint(arguments: list[str], environment: dict[str, str], cwd: Path,
                      root: Path) -> subprocess.CompletedProcess[str]:
    """Run the real CLI and current fake scripts, keeping Git as real processes."""
    real_run = subprocess.run

    def run(command: list[str], *, capture_output: bool = False, text: bool = False,
            check: bool = False, timeout: float | None = None, env: dict[str, str] | None = None,
            cwd: Path | str | None = None, stdin: int | None = None, stdout: int | None = None,
            stderr: int | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
        child_env = dict(os.environ) if env is None else env
        child_cwd = Path.cwd() if cwd is None else Path(cwd)
        executable = Path(shutil.which(command[0], path=child_env.get("PATH")) or command[0])
        script, args = executable, command[1:]
        if not script.is_relative_to(root) and executable.name.startswith("python") and len(command) > 1:
            script, args = Path(command[1]), command[2:]
        if executable.name != "git" and script.is_relative_to(root) and script.is_file() and script.read_text().startswith(f"#!{sys.executable}\n"):
            fake = ModuleType("checkpoint_tool_fixture")
            fake.__file__ = str(script)

            def main() -> int:
                exec(compile(script.read_text(), str(script), "exec"), {**vars(fake), "__name__": "__main__"})
                return 0

            return invoke_cli(fake, main, args, child_env, child_cwd)
        if script == Path(progress_history.__file__):
            return invoke_recorder(args, child_env, child_cwd)
        return real_run(command, capture_output=capture_output, text=text, check=check,
                        timeout=timeout, env=env, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr, input=input)

    with mock.patch.object(subprocess, "run", run), mock.patch.object(
        change_review, "lists_directory", return_value=Path(environment["HOME"]) / "rust/hanadocs/change-review",
    ):
        return invoke_cli(merge_checkpoint, merge_checkpoint.main, arguments, environment, cwd,
                          production_state=True)


# Script completion deadlines only bound hangs; startup can be slow under load.

SCRIPT = Path(__file__).with_name("merge_checkpoint.py")
STARTED = "2026-10-06T12:00:00-07:00"
TRIAL = ("review trial: ux 2 findings, code 3 findings, review-worker minutes 14, "
         "ux check minutes 4, ux repair minutes 1")
SIMPLE_MERGE_TESTS = "- **Merge tests:** `checkpoint-test`"
REAL_MERGE_TESTS = ("- **Merge tests:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` "
                    "and `basedpyright scripts/buildlog` (status zero and a clean summary), run in the showrunner checkout")
DELIVERS = "Delivers checkpoint merge behavior."
PARALLEL = "Parallel: none — no phase is waiting"
STUB = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state = Path(os.environ["CHECKPOINT_TEST_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
TEST_HANG_GUARD_SECS = __HANG_GUARD__
with (state / "events.jsonl").open("a") as output:
    output.write(json.dumps({"command": name, "args": args}) + "\\n")
if name == "ssh":
    print("rc=" + (state / "mac-rc").read_text().strip() if (state / "mac-rc").exists() else "rc=0")
    raise SystemExit(0)
if name == "style_sync.py":
    import subprocess
    repo = args[args.index("--repo-dir") + 1]
    branch = args[args.index("--branch") + 1]
    observed = subprocess.run(["git", "-C", repo, "ls-remote", "origin", "refs/heads/" + branch],
                              capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.split()[0]
    (state / "notified-tip").write_text(observed)
if name == "verify.sh":
    fail_rows = state / "fail-rows"
    if fail_rows.exists() and "--filter" not in args:
        print(fail_rows.read_text(), end="")
        raise SystemExit(1)
    key = "_".join(args).replace("/", "_")
    if args[:1] == ["test"]:
        package_key = "test-" + args[1] if len(args) > 1 else "test"
        package_marker = state / ("fail-once-" + package_key)
        if package_marker.exists():
            package_marker.unlink()
            raise SystemExit(1)
        if (state / ("fail-always-" + package_key)).exists():
            raise SystemExit(1)
        key = "test"
    marker = state / ("fail-once-" + key)
    if marker.exists():
        marker.unlink()
        raise SystemExit(1)
    if (state / ("fail-always-" + key)).exists():
        raise SystemExit(1)
if (state / ("fail-" + name)).exists():
    raise SystemExit(1)
if name == "basedpyright":
    print("0 errors, 0 warnings, 0 notes", flush=True)
    print("venv .venv subdirectory not found in venv path " + os.getcwd() + ".", file=sys.stderr)
    raise SystemExit(0)
'''.replace("__HANG_GUARD__", str(TEST_HANG_GUARD_SECS))


BUMP_TEST_STUB = STUB + '''
import subprocess
with open(Path(__TEST_REPOSITORY__) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.wait_for_test_state import wait_for_file
from scripts.message import sessions
from scripts.herdr.herdr import NATIVE_ID_MARKS

def git(*arguments):
    return subprocess.run(["git", *arguments], capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip()

gate_pid = os.getppid()
run = {"cwd": str(Path.cwd()), "head": git("rev-parse", "HEAD"),
       "origin": git("ls-remote", os.environ["BUMP_TEST_ORIGIN"], "refs/heads/production").split()[0],
       "branch": subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS).stdout.strip(),
       "gate_pid": gate_pid, "gate_start": sessions.process_start(gate_pid),
       "session": os.getsid(0), "stdin_closed": os.read(0, 1) == b""}
# A red command is retried inside the same gate. Observe runners, not attempts.
path = state / "gate-runs" / f"{gate_pid}-{run['gate_start']}.json"
temporary = path.with_suffix(".pending")
temporary.write_text(json.dumps(run))
temporary.replace(path)
if (state / "gate-blocked").exists():
    wait_for_file(state / "gate-release")
fail_once = state / "gate-fail-once"
if fail_once.exists():
    fail_once.unlink()
    raise SystemExit(1)
if (state / "gate-red").exists() and "-E" not in args:
    print("    FAIL [  0.1s] (1/1) fixture bump_contract_failed", flush=True)
    raise SystemExit(1)
'''.replace("__TEST_REPOSITORY__", repr(str(Path(__file__).resolve().parents[2])))

BUMP_SEND_STUB = STUB + '''
import uuid
if (state / "slow-delivery").exists():
    with open(Path(__TEST_REPOSITORY__) / "scripts/lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.wait_for_test_state import wait_for_file
    (state / "delivery-started").touch()
    wait_for_file(state / "delivery-release")
if (state / "stop-before-showrunner-notice").exists():
    with open(Path(__TEST_REPOSITORY__) / "scripts/lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
    from scripts.production import staff
    recipient = staff.local_snapshot().by_current_session(args[args.index("--to") + 1].removeprefix("session:"))
    if recipient is not None and recipient.kind == "showrunner":
        import signal
        os.kill(os.getppid(), signal.SIGKILL)
        raise SystemExit(1)
retry = state / "send-fail-once"
if retry.exists():
    retry.unlink()
    print("fixture send failed", file=sys.stderr, flush=True)
    raise SystemExit(1)
path = state / "gate-notices" / (uuid.uuid4().hex + ".json")
temporary = path.with_suffix(".pending")
temporary.write_text(json.dumps(args))
temporary.replace(path)
print("SENT: fixture", flush=True)
'''.replace("__TEST_REPOSITORY__", repr(str(Path(__file__).resolve().parents[2])))


class BumpGateObservation(NamedTuple):
    checkout: Path
    head: str
    origin: str
    branch: str
    gate_pid: int
    gate_start: str
    session: int
    stdin_closed: bool


def configure_test_environment() -> None:
    """Use Pacific time and private scratch and registry folders for each module."""
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {
        **config_environment(Path(folder.name) / "config"),
        "USER_ZONE": "America/Los_Angeles", "SCRATCH_ROOT": f"{folder.name}/scratch",
        "SCRATCH_VISIBLE_ROOT": f"{folder.name}/repro"}))
    _ = unittest.enterModuleContext(isolated_staff_file(Path(folder.name) / "staff.json", {}))


class MergeResult(NamedTuple):
    process: subprocess.CompletedProcess[str]
    before: str


class MergeCheckpointFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.origin: Path = Path()
        self.checkout: Path = Path()
        self.unit: Path = Path()
        self.other: Path = Path()
        self.promote: Path = Path()
        self.doc: Path = Path()
        self.log: Path = Path()
        self.bin: Path = Path()
        self.state: Path = Path()
        self.env: dict[str, str] = {}
        self.push: str = "git"
        self.promotions: str = ""
        self.known_flakes: str = ""
        self.known_flaky_tests: str = ""

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="checkpoint-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name).resolve() / "case"
        cls.fixture_snapshot = cls.fixture_root.with_name("snapshot")
        builder = cls()
        builder.bind_fixture(cls.fixture_root)
        with isolated_staff_file(Path(builder.env["STAFF_FILE"]), builder.env):
            builder.build_fixture()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        def cleanup() -> None:
            if self.fixture_root.exists():
                shutil.rmtree(self.fixture_root)

        self.addCleanup(cleanup)
        # Git's linked worktree metadata names absolute paths. Each case gets
        # private files, refs and origin restored at the seed's original path.
        if self._testMethodName == "runTest" and self.fixture_root.exists():
            # The provenance matrices create sequential nested fixtures. Their
            # outer fixture is unused; each matrix cell restores a fresh seed.
            shutil.rmtree(self.fixture_root)
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        _ = self.enterContext(isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env))

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "hana"
        self.unit = self.root / "alpha"
        self.other = self.root / "beta"
        self.promote = self.root / "installed"
        self.doc = self.root / "tool-based-ui-production.md"
        self.log = self.checkout / "production.log"
        self.bin = self.root / "bin"
        self.state = self.root / "stub-state"
        home = self.root / "home"
        self.env = {**os.environ, "CONF_DIR": str(self.root), "HOME": str(home), "STAFF_FILE": str(home / ".local/state/staff/staff.json"),
                    "GIT_CONFIG_GLOBAL": str(write_fixture_git_config(root.parent)), "GIT_CONFIG_NOSYSTEM": "1",
                    "MERGE_CHECKPOINT_STYLE_SYNC": str(self.bin / "style_sync.py"),
                    "MERGE_CHECKPOINT_STYLE_LEDGER": str(self.bin / "style_ledger.py"),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "CHECKPOINT_TEST_STATE": str(self.state),
                    "MERGE_CHECKPOINT_SCRATCH": str(self.state),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunners"),
                    "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "PLAN_DELEGATE_BOARD_DIR": "",
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
                    "MERGE_CHECKPOINT_RUN_ROOT": str(self.root / "unit-runs"),
                    "UNIT_HISTORY_DIR": str(self.root / "history"), "UNIT_SYNC_WRITES": "0",
                    "CLAUDE_PROBE_SESSION": ""}

    def build_fixture(self) -> None:
        self.root.mkdir()
        _ = config_environment(self.root)
        self.bin.mkdir()
        self.state.mkdir()
        self.checkout.mkdir()
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.checkout)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.checkout)
        _ = self.git("remote", "add", "origin", str(self.origin), cwd=self.checkout)
        self.write(self.checkout, "README.md", "base\n")
        self.write(self.checkout, "Cargo.toml", "[workspace]\n")
        self.write(self.checkout, ".gitignore", "production.log\n.unit.toml\n")
        self.write(self.checkout, "src/alpha.txt", "alpha base\n")
        self.write(self.checkout, "src/beta.txt", "beta base\n")
        _ = self.git("add", ".", cwd=self.checkout)
        _ = self.git("commit", "-m", "initial", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "main", cwd=self.checkout)
        _ = self.git("branch", "production", cwd=self.checkout)
        _ = self.git("checkout", "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "production", cwd=self.checkout)
        _ = self.git("worktree", "add", "-b", "alpha-branch", str(self.unit), "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "alpha-branch", cwd=self.unit)
        _ = self.git("worktree", "add", "-b", "beta-branch", str(self.other), "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "beta-branch", cwd=self.other)
        _ = self.git("worktree", "add", str(self.promote), "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.unit)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.unit)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.other)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.other)
        for name in ("verify.sh", "checkpoint-test", "validate_and_push.sh", "review_regime.py", "ssh",
                     "style_sync.py", "style_ledger.py"):
            self.executable(self.bin / name, STUB)
        home = self.root / "home"
        (home / "rust/hanadocs/change-review").mkdir(parents=True)
        for relative, name in ((".claude/scripts/direct", "verify.sh"),
                               (".claude/scripts/validate_and_push", "validate_and_push.sh"),
                               (".claude/scripts/production", "review_regime.py")):
            directory = home / relative
            directory.mkdir(parents=True)
            self.executable(directory / name, STUB)
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            _ = fixture_member("showrunner", name="example")
        self.production_doc()
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            owner = fixture_member("showrunner", name="example", doc=staff.LiveDoc(remote.local_machine(), self.doc))
            for name, folder in (("alpha", self.unit), ("beta", self.other)):
                _ = fixture_member("unit", name=name, parent=owner.id,
                                   worktree=staff.PresentWorktree(remote.local_machine(), folder))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout,
                                env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def write(self, checkout: Path, relative: str, content: str) -> None:
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def executable(self, path: Path, content: str) -> None:
        content = content.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
        _ = path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def production_doc(self, *, merge_tests_line: str | None = SIMPLE_MERGE_TESTS,
                       change_review: Path | None = None) -> None:
        for folder, plan in ((self.unit, "alpha"), (self.other, "beta")):
            (folder / "docs/plans").mkdir(parents=True, exist_ok=True)
            _ = (folder / ".unit.toml").write_text(
                f'plan = "docs/plans/{plan}.md"\nresponsibilities = []\n', encoding="utf-8")
        rows = [
            "# Production — example", "", "## Production Context", "",
            f"- **Repository:** `{self.checkout}`",
            "- **Merge branch:** `production`",
            *([f"- **Change review:** `{change_review}`"] if change_review else []),
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** test-showrunner",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
        ]
        if merge_tests_line is not None:
            rows.append(merge_tests_line)
        if self.push:
            rows.append(f"- **Push:** {self.push}")
        if self.promotions:
            rows.append(f"- **Promote:** {self.promotions}")
        if self.known_flakes:
            rows.append(f"- **Known flakes:** {self.known_flakes}")
        if self.known_flaky_tests:
            rows.append(f"- **Known flaky tests:** {self.known_flaky_tests}")
        rows.extend(["", "## Production rules", "", "- Merge each phase and push the merge branch."])
        _ = self.doc.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def checkpoint(self, relative: str = "src/alpha.txt", content: str = "alpha change\n",
                   *, subject: str = "alpha work", push: bool = True) -> str:
        self.write(self.unit, relative, content)
        _ = self.git("add", relative, cwd=self.unit)
        _ = self.git("commit", "-m", subject, cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        if push:
            _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return sha

    def push_main_change(self, relative: str, content: str) -> str:
        """Push an independent main fix through the fixture's private origin."""
        writer = self.root / "main-fix-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=writer)
        self.write(writer, relative, content)
        _ = self.git("add", relative, cwd=writer)
        _ = self.git("commit", "-m", "independent main fix", cwd=writer)
        _ = self.git("push", "origin", "main", cwd=writer)
        return self.git("rev-parse", "HEAD", cwd=writer)

    def run_checkpoint(self, sha: str, *extra: str, phase: str = "2",
                       review: bool = True, trial: str = TRIAL,
                       delivers: str | None = DELIVERS, parallel: str | None = PARALLEL) -> MergeResult:
        before = self.git("rev-parse", "HEAD")
        runs = self.root / "history/runs"
        fixture_records = runs / "fixture.jsonl"
        if "--shrink" not in extra and "--as-built" not in extra and not any(
                path != fixture_records for path in runs.glob("*.jsonl")):
            runs.mkdir(parents=True, exist_ok=True)
            with fixture_records.open("a") as output:
                _ = output.write(json.dumps({"schema_version": 1, "event_type": "checkpoint",
                    "working_dir": str(self.unit), "checkpoint_commit": sha,
                    "phase_id": phase, "timestamp_epoch": fixture_records.stat().st_size}) + "\n")
        arguments = ["--production", "example", "alpha", phase, sha,
                     "--started", STARTED, "--scratch", str(self.root / "scratch"),
                     "--trailer", "Co-authored-by: Test <test@example.invalid>"]
        if review:
            arguments.extend(("--review-trial", trial))
        if parallel is not None and "--shrink" not in extra and "--as-built" not in extra:
            arguments.extend(("--parallel", parallel))
        if delivers is not None and "--shrink" not in extra and "--as-built" not in extra:
            arguments.extend(("--delivers", delivers))
        arguments.extend(extra)
        process = self.invoke(arguments)
        return MergeResult(process, before)

    def prepare_bump_gate(self, *, red: bool = False, blocked: bool = True) -> None:
        """Use the normal merge fixture with file handshakes and private recipients."""
        for name in ("gate-runs", "gate-notices"):
            (self.state / name).mkdir()
        for enabled, marker in ((red, "gate-red"), (blocked, "gate-blocked")):
            if enabled:
                (self.state / marker).touch()
        self.executable(self.bin / "checkpoint-test", BUMP_TEST_STUB)
        self.executable(self.bin / "send.py", BUMP_SEND_STUB)
        self.env["MERGE_CHECKPOINT_SEND"] = str(self.bin / "send.py")
        self.env["BUMP_TEST_ORIGIN"] = str(self.origin)
        self.env.update(fake_unit_host.environment(self.state))
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            production_membership("example", "alpha")
            runner = fixture_member("showrunner", name="example")
            director = unit_membership("example", "alpha")
            fake_unit_host.write(self.env, [labeled_host("fixture", "w1:p1", "alpha", production_name="example")])
        records = self.root / "sessions"
        records.mkdir()
        self.env["NOTIFIER_SESSIONS_DIR"] = str(records)
        sockets = Path(self.enterContext(short_socket_directory()))
        for name, pid in (("alpha", os.getpid()), ("showrunner", fake_unit_host.live_pid(self))):
            address = sockets / (name + ".sock")
            held = socket.socket(socket.AF_UNIX)
            held.bind(str(address))
            self.addCleanup(held.close)
            if name == "alpha":
                fake_unit_host.place_process(self.env, pid, "w1:p1")
            _ = (records / (str(pid) + ".json")).write_text(json.dumps({
                "pid": pid, "sessionId": director.id if name == "alpha" else runner.id, "name": name,
                "messagingSocketPath": str(address), "updatedAt": 1,
                "procStart": sessions.process_start(pid), "tmux": "",
            }))
        timer = self.root / f"notifier/showrunner-{runner.id}"
        timer.mkdir(parents=True)
        _ = (timer / "conf").write_text(
            f"TARGET=session:{runner.id}\nCHECK=zsh /fixture/production_check.sh {runner.id}\n"
        )
        self.addCleanup(self.stop_bump_gates)

    def refuse_bump_clone(self) -> None:
        git = shutil.which("git")
        assert git is not None
        self.executable(self.bin / "git", "#!/usr/bin/env python3\nimport os, sys\n"
                        + "arguments = sys.argv[1:]\n"
                        + "while arguments[:1] in (['-c'], ['-C']): arguments = arguments[2:]\n"
                        + "if arguments[:1] == ['clone']:\n"
                        + "    print('fixture clone refused', file=sys.stderr)\n    raise SystemExit(1)\n"
                        + f"os.execv({git!r}, [{git!r}, *sys.argv[1:]])\n")

    @contextmanager
    def refuse_bump_gate_launch(self) -> Generator[None]:
        original = subprocess.Popen

        def launch(command: Sequence[str], *, stdin: int | None = None,
                   stdout: int | IO[str] | None = None, stderr: int | IO[str] | None = None,
                   cwd: str | Path | None = None, env: Mapping[str, str] | None = None,
                   text: bool = False, start_new_session: bool = False) -> subprocess.Popen[str] | subprocess.Popen[bytes]:
            if any(Path(word).name == "bump_gate.py" for word in command):
                raise OSError("fixture gate launch refused")
            return original(command, stdin=stdin, stdout=stdout, stderr=stderr, cwd=cwd, env=env,
                            text=text, start_new_session=start_new_session)

        with mock.patch.object(subprocess, "Popen", side_effect=launch):
            yield

    def move_bump_recipients(self) -> None:
        timer = next((self.root / "notifier").glob("showrunner-*/conf"))
        timer_text = timer.read_text()
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            for path in (self.root / "sessions").glob("*.json"):
                row = cast(dict[str, object], json.loads(path.read_text()))
                old_id = staff.native_id(cast(str, row["sessionId"]))
                new_id = staff.native_id(fixture_runtime_id("moved-recipient:" + old_id))
                _ = staff.update_current_session(old_id, new_id)
                for environment in Path(self.env["HERDR_PROC"]).glob("*/environ"):
                    parts = environment.read_bytes().split(b"\0")
                    for index, part in enumerate(parts):
                        key, separator, value = part.partition(b"=")
                        if key.decode() in NATIVE_ID_MARKS and value.decode() == old_id:
                            parts[index] = key + separator + new_id.encode()
                    _ = environment.write_bytes(b"\0".join(parts))
                row["sessionId"] = new_id
                row["name"] = cast(str, row["name"]) + "-moved"
                _ = path.write_text(json.dumps(row))
        _ = timer.write_text(timer_text)

    def launch_competing_bump_gate(self, tip: str) -> subprocess.Popen[bytes]:
        path = merge_checkpoint.checkpoint_step_record(self.checkout, tip, "bump-gate")
        output = self.enterContext((self.state / "competing-gate.log").open("wb"))
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT.with_name("bump_gate.py")), "--record", str(path)],
            cwd=self.checkout, env=self.env, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process_group.send_process_group(process.pid, signal.SIGKILL)
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

        self.addCleanup(cleanup)
        return process

    def wait_competing_bump_gate(self, process: subprocess.Popen[bytes]) -> None:
        wait_until(lambda: process.poll() is not None or len(self.bump_gate_runs()) > 1,
                   self.state / "gate-runs", processes=(process,))

    def bump_gate_record(self, tip: str) -> dict[str, object]:
        path = merge_checkpoint.checkpoint_step_record(self.checkout, tip, "bump-gate")
        return cast(dict[str, object], json.loads(path.read_text()))

    def assert_main_push_lock_available(self) -> None:
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        with (common / "main-push.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)

    def wait_bump_result(self, tip: str) -> dict[str, object]:
        path = merge_checkpoint.checkpoint_step_record(self.checkout, tip, "bump-gate")
        return self.wait_bump_result_at(path)

    def wait_bump_result_at(self, path: Path) -> dict[str, object]:
        wait_until(lambda: path.exists() and checkpoint_gate.read_record(path)["result"] in ("red", "green"), path.parent)
        record = cast(dict[str, object], json.loads(path.read_text()))
        pid = cast(int, record["pid"])
        if pid:
            self.wait_bump_process_exit(pid)
        return record

    def bump_gate_runs(self) -> list[BumpGateObservation]:
        runs: list[BumpGateObservation] = []
        for path in sorted((self.state / "gate-runs").glob("*.json")):
            row = cast(dict[str, object], json.loads(path.read_text()))
            runs.append(BumpGateObservation(
                Path(cast(str, row["cwd"])), cast(str, row["head"]), cast(str, row["origin"]),
                cast(str, row["branch"]), cast(int, row["gate_pid"]), cast(str, row["gate_start"]),
                cast(int, row["session"]), cast(bool, row["stdin_closed"]),
            ))
        return runs

    def wait_bump_gate_runs(self, count: int) -> list[BumpGateObservation]:
        wait_until(lambda: len(self.bump_gate_runs()) >= count, self.state / "gate-runs")
        return self.bump_gate_runs()

    def bump_notices(self) -> list[list[str]]:
        return [cast(list[str], json.loads(path.read_text()))
                for path in sorted((self.state / "gate-notices").glob("*.json"))]

    def release_bump_gate(self) -> None:
        (self.state / "gate-release").touch()

    def wait_bump_gate_exit(self, run: BumpGateObservation) -> None:
        self.wait_bump_process_exit(run.gate_pid)

    def wait_bump_process_exit(self, pid: int) -> None:
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            self.assertTrue(select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)[0],
                            "background gate did not finish")
        finally:
            os.close(descriptor)
        try:
            _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass

    def stop_bump_gate(self, run: BumpGateObservation) -> None:
        if run.checkout == self.checkout:
            return
        if isinstance(sessions.process_liveness(run.gate_pid, run.gate_start), sessions.LiveSessionProcess):
            self.assertEqual(os.getsid(run.gate_pid), run.gate_pid, "fixture gate must own its session")
            process_group.send_process_group(run.gate_pid, signal.SIGKILL)
            try:
                _ = os.waitpid(run.gate_pid, 0)
            except ChildProcessError:
                pass
        if run.checkout.exists():
            shutil.rmtree(run.checkout)

    def stop_bump_gates(self) -> None:
        for run in self.bump_gate_runs():
            self.stop_bump_gate(run)

    def events(self, command: str) -> list[list[str]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        rows = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [cast(list[str], row["args"]) for row in rows if row["command"] == command]

    def assert_stopped_before_merge(self, result: MergeResult, state: str, detail: str) -> None:
        self.assertIn(f": {state} —", result.process.stdout, result.process.stdout + result.process.stderr)
        self.assertIn(detail, result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertEqual(self.events("review_regime.py"), [])
        self.assertIn("send alpha:", result.process.stdout)

    def receipts(self) -> list[dict[str, object]]:
        path = self.root / "history" / "merge_receipts.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def record_history(self, *arguments: str) -> dict[str, object]:
        result = invoke_recorder(arguments, self.env, self.unit)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, object], json.loads(result.stdout)) if arguments[0] == "waits" else {}

    def start_history_run(self, name: str, worktree: Path) -> Path:
        session = self.root / "unit-runs" / name
        _ = self.record_history("start-run", "--session-dir", str(session), "--working-dir", str(worktree),
                                "--main-family", "codex", "--main-model", "fixture", "--main-session-id", "director")
        return session

    def assert_checkpoint_provenance(self, *, across_runs: bool, second_active: bool, checkpoint_events: int) -> None:
        checkpoint = self.checkpoint()
        first = self.start_history_run("first", self.unit)
        _ = self.record_history("start-phase", "--session-dir", str(first), "--phase-id", "2", "--phase-title", "First")
        if checkpoint_events:
            _ = self.record_history("checkpoint", "--session-dir", str(first), "--phase", "2", "--commit", checkpoint)
        _ = self.record_history("finish-phase", "--session-dir", str(first), "--phase-id", "2", "--status", "completed")
        if across_runs:
            _ = self.record_history("finish-run", "--session-dir", str(first), "--status", "completed")
        second = self.start_history_run("second", self.unit) if across_runs else first
        _ = self.record_history("start-phase", "--session-dir", str(second), "--phase-id", "2", "--phase-title", "Second")
        if checkpoint_events == 2:
            _ = self.record_history("checkpoint", "--session-dir", str(second), "--phase", "2", "--commit", checkpoint)
        if not second_active:
            _ = self.record_history("finish-phase", "--session-dir", str(second), "--phase-id", "2", "--status", "completed")
        merged = self.run_checkpoint(checkpoint)
        self.assertEqual(merged.process.returncode, 0, merged.process.stdout + merged.process.stderr)
        report = self.record_history("waits")
        phases = cast(list[dict[str, object]], report["phases"])
        waits = cast(list[dict[str, object]], report["waits"])
        self.assertEqual(len(phases), 2)
        if checkpoint_events == 1:
            self.assertEqual(len(waits), 1)
            self.assertEqual((waits[0]["run_id"], waits[0]["phase_instance_id"]),
                             (phases[0]["run_id"], phases[0]["phase_instance_id"]))
            self.assertEqual([phase["wait_n"] for phase in phases], [1, 0])
        else:
            self.assertEqual(waits, [])
            self.assertEqual(report["missing_record_n"], 1)
            for phase in phases:
                self.assertEqual(phase["state"], "UncertainCoverage")
                self.assertNotIn("wait_n", phase)
                self.assertTrue(phase["reason"])
                self.assertTrue(phase["candidates"])

    def dated_checkpoint(self, message: str) -> str:
        """A checkpoint(...) commit on the unit branch, dated after the Change-Review rule landed."""
        self.write(self.unit, "src/alpha.txt", message)
        _ = self.git("add", "src/alpha.txt", cwd=self.unit)
        _ = subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.unit, check=True,
                           env={**self.env, "GIT_COMMITTER_DATE": "2026-10-15T12:00:00-04:00"}, timeout=TEST_HANG_GUARD_SECS)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return self.git("rev-parse", "HEAD", cwd=self.unit)

    def as_built_commit(self, *extra: str) -> str:
        """Land the plan through a shrink, then delete it beside a new as-built doc."""
        shrink = self.checkpoint("docs/plans/alpha.md", "plan\n", subject="shrink")
        landed = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertEqual(landed.process.returncode, 0, landed.process.stderr + landed.process.stdout)
        _ = self.git("rm", "-q", "docs/plans/alpha.md", cwd=self.unit)
        self.write(self.unit, "docs/as-built/alpha.md", "as built\n")
        for path in extra:
            self.write(self.unit, path, "extra\n")
        _ = self.git("add", "docs/as-built/alpha.md", *extra, cwd=self.unit)
        _ = self.git("commit", "-m", "docs(alpha): as-built", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return self.git("rev-parse", "HEAD", cwd=self.unit)

    def child_production(self, *, parent: str = "example") -> Path:
        doc = self.root / "example-style-production.md"
        _ = doc.write_text("\n".join([
            "# Style production", "- **Kind:** style", "- **Branch point:** production",
            "- **Merge branch:** alpha-branch", f"- **Showrunner checkout:** {self.unit}",
            "- **Log:** style.log", "",
        ]))
        _ = (self.state / "child-parent").write_text(parent)
        registry = self.bin / "showrunners.py"
        self.executable(registry, "\n".join([
            "#!/usr/bin/env python3", "import sys", "from pathlib import Path",
            f"state = Path({str(self.state)!r})",
            "if sys.argv[1] == 'parent': print((state / 'child-parent').read_text())",
            f"elif sys.argv[1] == 'list': print('example-style\\t{doc}')",
            "elif sys.argv[1] == 'address': print('session:fake-child')",
            "else: raise SystemExit(2)", "",
        ]))
        self.executable(self.bin / "send.py", STUB)
        with isolated_staff_file(Path(self.env["STAFF_FILE"]), self.env):
            relationship = fixture_member("showrunner", name=parent).id if parent else staff.StaffRoot()
            _ = fixture_member("showrunner", name="example-style", parent=relationship,
                               doc=staff.LiveDoc(remote.local_machine(), doc))
        self.env["MERGE_CHECKPOINT_SHOWRUNNERS"] = str(registry)
        self.env["MERGE_CHECKPOINT_SEND"] = str(self.bin / "send.py")
        return doc

    def run_handoff(self, checkpoint: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.invoke(["--from-child", "example-style", "--checkpoint", checkpoint,
                            "--production", "example", "--started", STARTED, *extra])

    def prepare_handoff_ledger(self, checkpoint: str, *, add_handoff: bool = True) -> None:
        ledger = Path(__file__).resolve().parents[1] / "style/style_ledger.py"
        self.env["MERGE_CHECKPOINT_STYLE_LEDGER"] = str(ledger)
        self.env["STYLE_LEDGER_STATE_DIR"] = str(self.root / "style-state")
        catalog = self.root / "rule-catalog"
        catalog.mkdir()
        self.env["STYLE_HISTORY_NATE_STYLE_DIR"] = str(catalog)
        commands = [["reconcile", "--branch-point", "production", "--style-branch", "alpha-branch"]]
        if add_handoff:
            commands.extend([
                ["handoff", "add", "--checkpoint", checkpoint],
                ["handoff", "send", "--checkpoint", checkpoint, "--to", "example"],
            ])
        for arguments in commands:
            result = subprocess.run(
                [sys.executable, str(ledger), *arguments, "--repo", str(self.unit)],
                env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def handoff_ledger_row(self, checkpoint: str) -> tuple[str, str]:
        database = self.root / "style-state" / self.checkout.name / "ledger.sqlite"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                "SELECT state,merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchall())
        self.assertEqual(len(rows), 1)
        return rows[0]

    def invoke(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        if process_boundary_for_case(self):
            return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                  env=self.env, capture_output=True, text=True, check=False,
                                  timeout=TEST_HANG_GUARD_SECS)
        return invoke_checkpoint(arguments, self.env, self.checkout, self.root)
