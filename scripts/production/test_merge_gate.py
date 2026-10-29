"""Pin the merge gate's contract using disposable repositories and fake tools."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
import json
import contextlib
import io
import os
import fcntl
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import TypedDict, cast, final, override
import unittest
from contextlib import redirect_stdout
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.production import change_review, merge_gate
from scripts.direct.wait_for_test_state import wait_for_file
from scripts.direct.local_release_wait import IntervalReleasePolling
from scripts.build_hold.test_hold_applies import TemporaryHoldTest
from scripts.buildlog.store import host_name


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("merge_gate.py")
EXPECTED_SUITES = (
    "lib", "hooks", "message", "accounts", "whoami", "agents", "build_hold", "build_queue",
    "mac_test", "usage", "bevy_upgrade", "bevy_migration_plan", "production", "direct", "buildlog",
    "lint", "nightly_review", "build_targets", "shutdown", "feature_log",
    "model_study", "herdr", "notify", "statusline", "astra", "style", "history", "guilds", "issue_score",
)
# Each suite's fixture holds test_first.py, and lib also test_second.py.
MODULES = len(EXPECTED_SUITES) + 1

TOOL_STUB = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

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
        import time
        deadline = time.monotonic() + 300
        while not (state / "modules.jsonl").exists():
            if time.monotonic() >= deadline:
                raise SystemExit("type job never overlapped a module")
            time.sleep(0.01)
    if (state / "types-hold").exists():
        # Outlive several 0.25 s memory samples so the runner records this job's peak.
        import time
        time.sleep(1.5)
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
'''

SUITE_MODULE = '''import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
import unittest

state = Path(os.environ["MERGE_GATE_TEST_STATE"])
module = Path(__file__)
suite = module.parent.name

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
        record({"suite": suite, "module": module.name, "pid": os.getpid(),
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
            deadline = time.monotonic() + 300
            while not (state / (str(os.getpid()) + ".released")).exists():
                if time.monotonic() >= deadline:
                    self.fail("runner never released the module")
                time.sleep(0.01)
        attempt = state / (suite + "-" + module.name + ".attempt")
        first_run = not attempt.exists()
        attempt.write_text("ran")
        if (state / ("fail-first-" + suite)).exists() and first_run:
            self.fail("fixture first run in " + suite + "/" + module.name)
        if not first_run:
            print("fixture rerun in " + suite + "/" + module.name)
        if (state / "leak-child").exists() and suite == "hooks":
            import subprocess
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
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
            while True:
                time.sleep(0.01)
        if (state / "grow-memory").exists() and suite == "hooks":
            # Grows until the runner kills it; past 2 GiB the cap failed to stop it.
            blocks = []
            while len(blocks) < 256:
                blocks.append(b"x" * (8 << 20))
                time.sleep(0.02)
            self.fail("fixture grew to 2 GiB and was never killed")
        if (state / "barrier").exists() and suite == "lib":
            deadline = time.monotonic() + 300
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
        record({"suite": suite, "module": module.name, "pid": os.getpid(),
                "event": "finish", "tmpdir": str(tmp)})
'''


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


@final
class MergeGateTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.origin = Path()
        self.checkout = Path()
        self.unit = Path()
        self.main = Path()
        self.bin = Path()
        self.state = Path()
        self.quiet_production = Path()
        self.environment: dict[str, str] = {}
        self.base = ""
        self.checkpoint = ""
        self.settings_blob = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="merge-gate-test-")))
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "merge"
        self.unit = self.root / "unit"
        self.main = self.root / "main"
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.bin.mkdir()
        self.state.mkdir()
        home = self.root / "home"
        home.mkdir()
        self.environment = {
            **os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / "state"),
            "CODEX_HOME": str(home / "codex"), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "MERGE_GATE_TEST_STATE": str(self.state),
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
        # A production with no Change review line: a gate always names one, and these write no rows.
        self.quiet_production = self.root / "quiet-production.md"
        self.write(self.quiet_production, "# quiet\n")
        self.environment["MERGE_GATE_TEST_PYTHONPATH"] = self.environment.get("PYTHONPATH", "")
        self.environment["PYTHONPATH"] = self.environment["MERGE_GATE_TEST_PYTHONPATH"]
        _ = self.git(self.root, "init", "--bare", str(self.origin))
        _ = self.git(self.root, "init", "-b", "main", str(self.checkout))
        _ = self.git(self.checkout, "config", "user.name", "Merge Gate Fixture")
        _ = self.git(self.checkout, "config", "user.email", "gate@example.invalid")
        _ = self.git(self.checkout, "remote", "add", "origin", str(self.origin))
        self.write(self.checkout / "settings.json", '{"fixture": true}\n')
        self.write(self.checkout / "subject.txt", "base\n")
        for suite in EXPECTED_SUITES:
            self.write(self.checkout / "scripts" / suite / "test_first.py", SUITE_MODULE)
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
        deadline = time.monotonic() + 300
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
                                check=False, timeout=300)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def commit(self, checkout: Path, subject: str) -> str:
        _ = self.git(checkout, "add", ".")
        _ = self.git(checkout, "commit", "-m", subject)
        return self.git(checkout, "rev-parse", "HEAD")

    def run_gate(self, *extra: str, checkpoint: str = "", previous: str = "",
                 ignored_signals: bool = False, callers_production: bool = False) -> subprocess.CompletedProcess[str]:
        prefix = [sys.executable, str(SCRIPT)]
        if ignored_signals:
            prefix = [sys.executable, "-c",
                      "import os, signal, sys; "
                      + "[signal.signal(sig, signal.SIG_IGN) for sig in "
                      + "(signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)]; "
                      + "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])", str(SCRIPT)]
        return subprocess.run(
            [*prefix, "--repo", str(self.checkout),
             "--merge-branch", "integration", "--unit-branch", "unit-phase",
             "--checkpoint", checkpoint or self.checkpoint,
             "--previous-checkpoint", previous or self.base,
             "--unit", "fixture-unit", "--phase", "phase-2",
             "--body", "Fixture checkpoint gate.", "--type-dirs", "scripts/production",
             "--settings-blob", self.settings_blob, "--main-repo", str(self.main),
             "--ssh", str(self.bin / "ssh"),
             *([] if callers_production or "--production" in extra else ["--production", str(self.quiet_production)]),
             *extra], cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=300,
        )

    def run_promote_only(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.checkout), "--merge-branch", "integration",
             "--promote-only", "--settings-blob", self.settings_blob, "--main-repo", str(self.main),
             "--ssh", str(self.bin / "ssh")], cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=300)

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
        self.assertCountEqual(starts, [(suite, "test_first.py") for suite in EXPECTED_SUITES]
                              + [("lib", "test_second.py")])

    def test_clean_pass_records_tree_commit_host_time_checkout_suites_and_source(self) -> None:
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["tree"], self.git(self.checkout, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(record["commit"], self.base)
        self.assertEqual(record["host"], host_name())
        self.assertEqual(record["checkout"], str(self.checkout))
        self.assertEqual(record["suites"], sorted(EXPECTED_SUITES))
        self.assertEqual(record["source"], "tests-only")
        at = datetime.fromisoformat(cast(str, record["at"]))
        self.assertEqual(at.utcoffset(), UTC.utcoffset(at))

    def test_dirty_and_failed_runs_do_not_record_a_pass(self) -> None:
        for state in ("tracked", "staged", "untracked", "failed"):
            with self.subTest(state=state):
                self.environment["MERGE_GATE_PASSED_DIR"] = str(self.state / state)
                self.clear_gate_events()
                if state == "tracked":
                    self.write(self.checkout / "subject.txt", "edited\n")
                elif state == "staged":
                    _ = self.git(self.checkout, "add", "subject.txt")
                elif state == "untracked":
                    _ = self.git(self.checkout, "restore", "--staged", "--worktree", "subject.txt")
                    self.write(self.checkout / "notes", "untracked\n")
                else:
                    (self.checkout / "notes").unlink()
                    self.write(self.state / "red-hooks", "red")
                result = self.run_gate("--tests-only")
                self.assertEqual(result.returncode, 1 if state == "failed" else 0, result.stdout + result.stderr)
                self.assertEqual(self.pass_records(), [])
                self.assertTrue(self.module_events())

    def test_second_tests_only_run_reuses_without_starting_any_module(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        self.environment["USER_ZONE"] = "America/New_York"
        second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        record, = self.pass_records()
        when = datetime.fromisoformat(cast(str, record["at"])).astimezone(
            ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")
        self.assertIn(f"tests reused from {cast(str, record['tree'])[:12]} ({when}, tests-only, {self.checkout})", second.stdout)

    def test_checkpoint_reuses_unit_tree_and_still_runs_types_and_checks_settings(self) -> None:
        first = self.run_gate("--tests-only", "--repo", str(self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        second = self.run_gate()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertTrue((self.state / "types-ran").exists())
        self.assertIn("tests reused from", second.stdout)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.git(self.checkout, "rev-parse", "HEAD"))
        self.clear_gate_events()
        refused = self.run_gate("--settings-blob", "00000000")
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("tests reused from", refused.stdout)
        self.assertIn("settings.json differs", refused.stderr)
        self.assertEqual(self.module_events(), [])

    def test_checkpoint_full_pass_records_and_tests_only_reuses_it(self) -> None:
        first = self.run_gate()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["source"], "checkpoint")
        self.assertEqual(record["commit"], self.git(self.checkout, "rev-parse", "HEAD"))
        self.clear_gate_events()
        second = self.run_gate("--tests-only")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.module_events(), [])
        self.assertIn(", checkpoint, ", second.stdout)

    def test_tree_dirty_host_or_suite_mismatch_runs_all_modules(self) -> None:
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original = self.pass_records()[0]
        path = Path(self.environment["MERGE_GATE_PASSED_DIR"]) / "passed-trees.jsonl"
        for mismatch in ("host", "suites", "dirty", "tree"):
            with self.subTest(mismatch=mismatch):
                record = dict(original)
                if mismatch == "host":
                    record["host"] = "other-host"
                elif mismatch == "suites":
                    record["suites"] = [*EXPECTED_SUITES, "other-suite"]
                else:
                    self.write(self.checkout / "subject.txt", "new tree\n")
                    if mismatch == "tree":
                        _ = self.commit(self.checkout, "different tree")
                self.write(path, json.dumps(record) + "\n")
                self.clear_gate_events()
                result = self.run_gate("--tests-only")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("tests reused from", result.stdout)
                self.assert_all_modules_started()

    def test_unavailable_ledger_runs_all_modules_and_reports_once(self) -> None:
        directory = Path(self.environment["MERGE_GATE_PASSED_DIR"])
        first = self.run_gate("--tests-only")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        path = directory / "passed-trees.jsonl"
        saved = path.read_text()
        for failure in ("unreadable", "unwritable", "invalid"):
            with self.subTest(failure=failure):
                self.write(path, "invalid json\n" if failure == "invalid" else saved)
                path.chmod(0o200 if failure == "unreadable" else 0o400 if failure == "unwritable" else 0o600)
                self.clear_gate_events()
                try:
                    result = self.run_gate("--tests-only")
                finally:
                    path.chmod(0o600)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.count("Test pass ledger unavailable;"), 1, result.stdout)
                self.assert_all_modules_started()

    def test_changed_checkout_or_failed_append_cannot_record_a_pass(self) -> None:
        finish = merge_gate.finish_gate_job
        for state in ("dirty-at-end", "dirty-at-start", "new-head", "append-fails"):
            with self.subTest(state=state):
                self.environment["MERGE_GATE_PASSED_DIR"] = str(self.state / state)
                self.clear_gate_events()
                if state == "dirty-at-start":
                    self.write(self.checkout / "subject.txt", "temporary edit\n")

                def finish_and_change(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
                    result = finish(job, status)
                    if state == "dirty-at-end":
                        self.write(self.checkout / "subject.txt", "edit during run\n")
                    elif state == "dirty-at-start":
                        _ = self.git(self.checkout, "restore", "subject.txt")
                    elif state == "new-head":
                        self.write(self.checkout / "subject.txt", "new commit during run\n")
                        _ = self.commit(self.checkout, "changed during tests")
                    else:
                        path = Path(self.environment["MERGE_GATE_PASSED_DIR"]) / "passed-trees.jsonl"
                        path.unlink()
                        path.mkdir()
                    return result

                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(merge_gate, "TEST_SUITES", ("hooks",)), \
                        mock.patch.object(merge_gate, "finish_gate_job", side_effect=finish_and_change), redirect_stdout(output):
                    run = merge_gate.run_suites(self.checkout)
                self.assertTrue(run.passed, output.getvalue())
                self.assertEqual(len(run.jobs), 1)
                if state == "append-fails":
                    self.assertEqual(output.getvalue().count("Test pass ledger unavailable;"), 1)
                else:
                    self.assertEqual(self.pass_records(), [])
                _ = self.git(self.checkout, "restore", "subject.txt")

    def test_reused_modules_do_not_hide_a_red_type_job(self) -> None:
        first = self.run_gate("--tests-only", "--repo", str(self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.clear_gate_events()
        self.write(self.state / "types-red", "red")
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests reused from", result.stdout)
        self.assertIn("basedpyright: RED", result.stdout)
        self.assertEqual(self.module_events(), [])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def assert_not_pushed(self) -> None:
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.base)

    def assert_refused_before_gate(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.module_events(), [])

    def test_dirty_merge_checkout_is_refused_without_destroying_changes(self) -> None:
        self.write(self.checkout / "subject.txt", "uncommitted\n")
        self.assert_refused_before_gate(self.run_gate())
        self.assertEqual((self.checkout / "subject.txt").read_text(), "uncommitted\n")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_untracked_showrunner_log_does_not_block_the_gate(self) -> None:
        self.write(self.checkout / "docs" / "plans" / "build-followups-log.md", "log\n")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.checkout / "docs" / "plans" / "build-followups-log.md").read_text(), "log\n")

    def test_missing_type_dir_is_refused_before_any_suite_runs(self) -> None:
        result = self.run_gate("--type-dirs", "accounts")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("type dirs not found", result.stderr)
        self.assertIn("accounts", result.stderr)
        self.assertEqual(self.module_events(), [])
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_merge_checkout_ahead_of_origin_is_refused(self) -> None:
        self.write(self.checkout / "local.txt", "local\n")
        local = self.commit(self.checkout, "unpublished merge work")
        self.assert_refused_before_gate(self.run_gate())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)

    def test_a_local_production_doc_commit_goes_out_with_the_merge(self) -> None:
        # retire_unit, add_unit and promote_showrunner leave their doc commit to the next merge push.
        self.write(self.checkout / "local.txt", "retired\n")
        local = self.commit(self.checkout, "production(fixture): retire alpha-unit")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Local doc commit goes out with this merge: {local[:12]}", result.stdout)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), head)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [head, local, self.checkpoint])

    def test_a_red_gate_keeps_the_local_production_doc_commit(self) -> None:
        self.write(self.checkout / "local.txt", "retired\n")
        local = self.commit(self.checkout, "production(fixture): retire alpha-unit")
        self.write(self.state / "modify-tracked", "modify")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)
        self.assert_not_pushed()

    def test_merge_checkout_behind_origin_is_refused(self) -> None:
        _ = self.git(self.unit, "push", "origin", "HEAD:integration")
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.checkpoint)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.calls(), [])

    def test_wrong_checkout_branch_is_refused(self) -> None:
        _ = self.git(self.checkout, "checkout", "-b", "wrong-branch")
        self.assert_refused_before_gate(self.run_gate())

    def test_checkpoint_not_on_remote_unit_branch_is_refused(self) -> None:
        self.write(self.unit / "unpublished.txt", "unpublished\n")
        unpublished = self.commit(self.unit, "unpublished checkpoint")
        self.assert_refused_before_gate(self.run_gate(checkpoint=unpublished))

    def test_missing_checkpoint_is_refused(self) -> None:
        self.assert_refused_before_gate(self.run_gate(checkpoint="does-not-exist"))

    def test_missing_previous_checkpoint_is_refused(self) -> None:
        self.assert_refused_before_gate(self.run_gate(previous="does-not-exist"))

    def test_previous_checkpoint_must_be_an_ancestor(self) -> None:
        self.write(self.unit / "later.txt", "later\n")
        later = self.commit(self.unit, "later checkpoint")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.assert_refused_before_gate(self.run_gate(previous=later))

    def test_conflict_aborts_merge_and_names_conflicting_files(self) -> None:
        self.write(self.unit / "subject.txt", "unit version\n")
        self.checkpoint = self.commit(self.unit, "unit conflict")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.write(self.checkout / "subject.txt", "merge version\n")
        self.base = self.commit(self.checkout, "merge conflict")
        _ = self.git(self.checkout, "push", "origin", "integration")
        result = self.run_gate(previous=self.git(self.unit, "rev-parse", "HEAD~1"))
        self.assert_refused_before_gate(result)
        self.assertIn("subject.txt", result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())

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

    def test_a_checkpoint_without_a_change_review_line_is_refused_before_the_merge(self) -> None:
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades")
        result = self.run_gate(checkpoint=checkpoint)
        self.assert_refused_before_gate(result)
        self.assertIn("checkpoint(fixture): grid fades: no Change-Review line", result.stderr)

    def review_list(self) -> tuple[Path, Path]:
        """A change-review list holding C7, and the production doc that names it."""
        review = self.root / "change-review.md"
        self.write(review, "## To review\n\n| # | Change | Merged | On the Mac build | How to check |\n"
                   + "| --- | --- | --- | --- | --- |\n| C7 | old | 2026-10-14 | yes | - |\n")
        doc = self.root / "production.md"
        self.write(doc, f"# p\n\n## Production Context\n\n- **Change review:** `{review}`\n")
        return review, doc

    def test_a_green_merge_adds_one_row_per_visible_change_to_the_named_list(self) -> None:
        review, doc = self.review_list()
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away\n"
                                            + "Change-Review: none — the gate script")
        # The row carries today in the user's zone, as the gate reads it; a run across midnight may take either day.
        before = change_review.today()
        result = self.run_gate("--production", str(doc), checkpoint=checkpoint)
        days = {before.isoformat(), change_review.today().isoformat()}
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merged = self.git(self.checkout, "rev-parse", "--short=8", "HEAD")
        written = review.read_text(encoding="utf-8").splitlines()[-1]
        rows = {f"| C8 | The grid fades gently | {day} · {merged} | not yet | Run `hana` in a natedev terminal. You see: The grid lines fade out farther away | production |" for day in days}
        self.assertIn(written, rows)
        self.assertIn(f"Change review: {written}", result.stdout)
        self.assertNotIn("drift", result.stderr)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_without_production_the_calling_showrunners_list_takes_the_rows(self) -> None:
        review, doc = self.review_list()
        timer = Path(self.environment["NOTIFIER_STATE_DIR"]) / "showrunner-fixture"
        timer.mkdir(parents=True)
        self.write(timer / "conf", "TARGET=session:fixture-showrunner\n"
                   + f"CHECK=bash /scripts/production/production_check.sh {doc}\n")
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away\n"
                                            + "Change-Review: The sky dims | Run `hana` in a natedev terminal | The sky turns darker")
        result = self.run_gate(checkpoint=checkpoint, callers_production=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Change review: 2 row(s) added to {review}: C8–C9", result.stdout)
        self.assertEqual([line.split(" | ")[1] for line in review.read_text(encoding="utf-8").splitlines()[-2:]],
                         ["The grid fades gently", "The sky dims"])

    def test_a_gate_that_finds_no_production_is_refused_before_the_merge(self) -> None:
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away")
        result = self.run_gate(checkpoint=checkpoint, callers_production=True)
        self.assert_refused_before_gate(result)
        self.assertIn("pass --production <doc>: the gate adds this merge's change-review rows to its list",
                      result.stderr)

    def test_a_production_without_a_list_still_merges_a_checkpoint_with_its_line(self) -> None:
        doc = self.root / "production.md"
        self.write(doc, "# p\n")
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\nChange-Review: none — the gate script")
        result = self.run_gate("--production", str(doc), checkpoint=checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Change review: the production doc has no Change review line; no rows written",
                      result.stdout)

    def test_green_gate_pushes_no_ff_merge_with_subject_body_and_all_modules(self) -> None:
        self.write(self.state / "types-barrier", "overlap")
        self.write(self.state / "types-hold", "hold")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), head)
        notifications = [call["args"] for call in self.calls() if call["tool"] == "style_sync.py"]
        self.assertEqual(notifications, [["notify", "--repo-dir", str(self.checkout), "--branch", "integration", "--tip", head]])
        self.assertEqual((self.state / "notified-tip").read_text(), head)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [head, self.base, self.checkpoint])
        message = self.git(self.checkout, "log", "-1", "--format=%B")
        self.assertIn(f"Merge fixture-unit phase-2 ({self.checkpoint}) into integration", message)
        self.assertIn("Fixture checkpoint gate.", message)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual([(row["suite"], row["module"]) for row in starts],
                              [(suite, "test_first.py") for suite in EXPECTED_SUITES] + [("lib", "test_second.py")])
        self.assertEqual(len({row["pid"] for row in starts}), MODULES)
        self.assertEqual(len({row["tmpdir"] for row in starts}), MODULES)
        self.assertTrue(all(not Path(row["tmpdir"]).exists() for row in starts))
        type_calls = [call for call in self.calls() if call["tool"] == "basedpyright"]
        self.assertEqual(len(type_calls), 1)
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual(type_calls[0]["args"], ["--pythonpath", str(venv / "bin" / "python"), "scripts/production"])
        peaks = cast(dict[str, float], json.loads((self.state / "runner" / "types_peak_mib.json").read_text()))
        self.assertEqual(list(peaks), ["scripts/production"])
        self.assertEqual(type_calls[0]["cwd"], str(self.checkout))
        for suite in EXPECTED_SUITES:
            self.assertRegex(result.stdout, rf"(?m)^{suite}: OK \d+\.\d+s$")
        peak = re.search(r"peak memory: ([\d.]+) GiB", result.stdout)
        self.assertIsNotNone(peak, result.stdout)
        assert peak is not None
        self.assertGreater(float(peak.group(1)), 0)
        self.assertRegex(result.stdout, r"(?m)^Largest module peak: [\d.]+ GiB \(sampled memory\); module: (?:\w+/test_\w+\.py|\(not sampled\))$")
        launches = [call for call in self.calls() if call["tool"] == "suite-python"]
        self.assertEqual(len(launches), MODULES)
        for call in launches:
            self.assertIn("discover", call["args"])
            self.assertEqual(call["cwd"], str(self.checkout))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_rejected_push_never_notifies_style(self) -> None:
        self.write(self.origin / "hooks/pre-receive", "#!/bin/sh\nexit 1\n")
        (self.origin / "hooks/pre-receive").chmod(0o755)
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertFalse(any(call["tool"] == "style_sync.py" for call in self.calls()))

    def test_style_notification_failure_keeps_the_green_merge_successful(self) -> None:
        self.write(self.state / "notify-fails", "fail")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("style sync: exit 1", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "integration"))

    def test_notification_value_error_keeps_the_completed_merge_successful(self) -> None:
        original_notify = merge_gate.notify_style

        def malformed_notification(repo: Path, branch: str, tip: str) -> None:
            with mock.patch("scripts.production.merge_gate.subprocess.run",
                            side_effect=ValueError("invalid production")):
                original_notify(repo, branch, tip)

        arguments = ["--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint,
                     "--previous-checkpoint", self.base, "--unit", "fixture-unit", "--phase", "phase-2",
                     "--body", "Fixture checkpoint gate.", "--type-dirs", "scripts/production",
                     "--settings-blob", self.settings_blob, "--main-repo", str(self.main),
                     "--ssh", str(self.bin / "ssh"), "--production", str(self.quiet_production)]
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
            merge_gate, "notify_style", side_effect=malformed_notification,
        ), redirect_stdout(output):
            code = merge_gate.main(arguments)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("style sync: invalid production", output.getvalue())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "integration"))

    def test_memory_cap_admits_two_overlapping_module_processes(self) -> None:
        self.write(self.state / "barrier", "two")
        result = self.run_gate("--tests-only", "--memory-cap-mib", "268", ignored_signals=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        active: set[int] = set()
        peak = 0
        events = self.module_events()
        for event in events:
            if event["event"] == "start":
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
        self.assertGreaterEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertEqual(len(events), 2 * MODULES)
        self.assertIn("sampled memory cap 268 MiB", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()
        self.assertFalse(any(call["tool"] in ("basedpyright", "ssh") for call in self.calls()))

    def test_running_job_limit_bounds_overlap_without_dropping_modules(self) -> None:
        self.write(self.state / "concurrency-barrier", "held")
        started: list[merge_gate.RunningGateJob] = []
        start = merge_gate.start_gate_job
        sample = merge_gate.process_memory_mib

        def start_blocked(suite: str, path: Path, argv: list[str], repo: Path,
                          environment: dict[str, str], log: Path) -> merge_gate.RunningGateJob:
            job = start(suite, path, argv, repo, environment, log)
            started.append(job)
            wait_for_file(self.state / f"{job.process.pid}.started")
            return job

        def release_at_sample(roots: set[int]) -> dict[int, float]:
            # No job can finish during the start loop. Without the job limit,
            # memory admission would start more than two before this sample.
            for job in started:
                (self.state / f"{job.process.pid}.released").touch()
            return sample(roots)

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "start_gate_job", side_effect=start_blocked), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=release_at_sample), \
                redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, max_running_jobs=2)
        self.assertTrue(run.passed, output.getvalue())
        expected = {(suite, "test_first.py") for suite in EXPECTED_SUITES}
        expected.add(("lib", "test_second.py"))
        self.assertEqual({(job.suite, job.module) for job in run.jobs}, expected)
        self.assertEqual(len(run.jobs), MODULES)
        active: set[int] = set()
        peak = 0
        events = self.module_events()
        for event in events:
            if event["event"] == "start":
                self.assertNotIn(event["pid"], active)
                active.add(event["pid"])
            else:
                active.remove(event["pid"])
            peak = max(peak, len(active))
            self.assertLessEqual(len(active), 2, events)
        self.assertEqual(peak, 2)
        self.assertEqual(active, set())
        self.assertEqual(len(events), 2 * MODULES)
        self.assertRegex(output.getvalue(), r"(?m)^Gate tests: [\d.]+s; at most 2 modules at once; peak memory:")
        self.assertEqual(output.getvalue().count("at most 2 modules at once"), 1)

    def test_smaller_memory_cap_serializes_modules_without_dropping_any(self) -> None:
        # Two jobs need two 96 MiB reservations, so a cap under 192 MiB runs one module at a time;
        # 128 MiB stays far above the measured peak of the runner and one module (about 48 MiB).
        result = self.run_gate("--tests-only", "--memory-cap-mib", "128")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.module_events()
        self.assertEqual(len(events), 2 * MODULES)
        for start, finish in zip(events[::2], events[1::2], strict=True):
            self.assertEqual((start["event"], finish["event"]), ("start", "finish"))
            self.assertEqual(start["pid"], finish["pid"])
        self.assertIn("sampled memory cap 128 MiB", result.stdout)

    def test_nonpositive_memory_cap_refuses_without_running_modules(self) -> None:
        result = self.run_gate("--tests-only", "--memory-cap-mib", "0")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    def test_missing_suite_refuses_instead_of_silently_shrinking_gate(self) -> None:
        (self.checkout / "scripts" / "notify" / "test_first.py").unlink()
        result = self.run_gate("--tests-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("notify", result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    def test_modules_preserve_the_callers_pythonpath(self) -> None:
        caller_path = self.root / "caller-pythonpath"
        caller_path.mkdir()
        self.environment.update({"PYTHONPATH": str(caller_path), "MERGE_GATE_TEST_PYTHONPATH": str(caller_path),
                                 "MERGE_GATE_TEST_ISOLATION": "1"})
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_module_tool_calls_fail_even_when_its_tests_pass(self) -> None:
        commands = [["cargo", "check"], ["cargo", "metadata"], ["rustc", "--version"],
                    ["rustdoc", "--version"], ["rustup", "show"], ["cargo-nextest", "--version"]]
        self.write(self.state / "tool-commands.json", json.dumps(commands))
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.state / "tool-statuses.json").read_text()), [99] * len(commands))
        for tool, argument in commands:
            self.assertIn(f"test_first.py: a test started real {tool}: {argument}", result.stdout)
        events = [event for event in self.module_events() if event["suite"] == "hooks"]
        self.assertEqual([event["event"] for event in events], ["start", "finish"])
        self.assertNotIn("Rerun alone:", result.stdout)

    def test_module_can_put_its_own_build_tool_stub_first(self) -> None:
        self.write(self.state / "tool-commands.json", json.dumps([["cargo", "check"]]))
        self.write(self.state / "own-tool-stubs", "stub cargo")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.state / "tool-statuses.json").read_text()), [0])
        self.assertNotIn("a test started real", result.stdout)

    def test_modules_use_private_build_logs_and_remove_them(self) -> None:
        caller_log = self.root / "caller-buildlog"
        self.write(caller_log / "keep", "caller log")
        self.environment["BUILDLOG_DIR"] = str(caller_log)
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [cast(dict[str, str], json.loads(line))
                for line in (self.state / "buildlogs.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), MODULES)
        self.assertEqual(len({row["buildlog"] for row in rows}), MODULES)
        for row in rows:
            self.assertTrue(Path(row["buildlog"]).is_relative_to(row["tmpdir"]))
            self.assertFalse(Path(row["tmpdir"]).exists())
        self.assertEqual(list(caller_log.iterdir()), [caller_log / "keep"])
        self.assertEqual((caller_log / "keep").read_text(), "caller log")

    def test_red_modules_fail_again_print_both_outputs_and_reset(self) -> None:
        self.write(self.state / "red-lib", "red")
        self.write(self.state / "red-hooks", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        for suite, module in (("lib", "test_first.py"), ("lib", "test_second.py"), ("hooks", "test_first.py")):
            self.assertIn(f"fixture failure in {suite}/{module}", result.stdout + result.stderr)
            first = result.stdout.index(f"--- {suite}/{module} first run exit=1 ---")
            second = result.stdout.index(f"--- {suite}/{module} rerun alone exit=1 ---")
            self.assertLess(first, second)
            self.assertIn(f"fixture failure in {suite}/{module}", result.stdout[first:second])
            self.assertIn(f"fixture rerun in {suite}/{module}", result.stdout[second:])
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), MODULES + 3)

    def test_failed_modules_pass_alone_and_keep_first_output_and_times(self) -> None:
        self.write(self.state / "fail-first-lib", "fail")
        self.environment["BUILDLOG_SCOPE"] = "1"
        output = io.StringIO()
        first_results: list[merge_gate.GateJobResult] = []
        finish = merge_gate.finish_gate_job

        def record_finish(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            result = finish(job, status)
            first_results.append(result)
            return result

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "in_builds_slice", return_value=False), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=record_finish), redirect_stdout(output):
            run = merge_gate.run_suites(self.checkout, type_dirs=("scripts/production",))
        report = output.getvalue()
        self.assertTrue(run.passed, report)
        self.assertEqual(len(run.jobs), MODULES + 1)
        for module in ("test_first.py", "test_second.py"):
            self.assertRegex(report, rf"Rerun alone: lib/{module} passed \(first run exit 1, [\d.]+ s\)")
            self.assertIn(f"--- lib/{module} passed on rerun alone; first run exit=1 ---", report)
            self.assertIn(f"fixture first run in lib/{module}", report)
        self.assertRegex(report, r"(?m)^lib: OK [\d.]+s$")
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), MODULES + 2)
        self.assertTrue(all(row["suite"] == "lib" for row in starts[MODULES:]))
        events = self.module_events()
        first_rerun = events.index(starts[MODULES])
        self.assertEqual(sum(row["event"] == "finish" for row in events[:first_rerun]), MODULES - 2)
        self.assertEqual([row["event"] for row in events[first_rerun:]], ["start", "finish", "start", "finish"])
        runner = self.state / "runner"
        timings = merge_gate.read_measures(runner / "module_seconds.json")
        history = merge_gate.read_history(runner / "module_history.json")
        for first in first_results[:MODULES + 1]:
            if first.suite != "types":
                key = f"{first.suite}/{first.module}"
                self.assertEqual(timings[key], first.seconds)
                self.assertEqual(history[key], [first.seconds])
        scopes = [call for call in self.calls() if call["tool"] == "systemd-run"]
        self.assertEqual(len(scopes), MODULES + 3)
        flags = ["--user", "--scope", "--quiet", "--collect", "--expand-environment=no", "--slice=builds.slice", "--"]
        self.assertEqual(scopes[0]["args"], [*flags, "true"])
        for call in scopes[1:]:
            self.assertEqual(call["args"][:len(flags)], flags)
        for call in scopes[-2:]:
            self.assertEqual(sum(previous["args"] == call["args"] for previous in scopes), 2)
        self.assertFalse(any("basedpyright" in " ".join(call["args"]) for call in scopes))

    def test_scope_skips_disabled_failed_probe_and_existing_builds_slice(self) -> None:
        for reason in ("disabled", "probe", "existing"):
            with self.subTest(reason=reason):
                self.environment["MERGE_GATE_PASSED_DIR"] = str(self.state / ("passes-" + reason))
                self.environment["BUILDLOG_SCOPE"] = "0" if reason == "disabled" else "1"
                self.write(self.state / "scope-fails", "fail")
                (self.state / "tools.jsonl").unlink(missing_ok=True)
                output = io.StringIO()
                with mock.patch.dict(os.environ, self.environment), \
                        mock.patch.object(merge_gate, "TEST_SUITES", ("hooks",)), \
                        mock.patch.object(merge_gate, "in_builds_slice", return_value=reason == "existing"), \
                        redirect_stdout(output):
                    run = merge_gate.run_suites(self.checkout)
                self.assertTrue(run.passed, output.getvalue())
                scopes = [call for call in self.calls() if call["tool"] == "systemd-run"]
                self.assertEqual([call["args"][-1] for call in scopes], ["true"] if reason == "probe" else [])
                message = {"disabled": "BUILDLOG_SCOPE=0", "probe": "probe failed (exit 1)",
                           "existing": "gate already runs in builds.slice"}[reason]
                self.assertIn("Module scope skipped: " + message, output.getvalue())

    def test_scope_probe_timeout_or_missing_command_runs_without_prefix(self) -> None:
        for error in (subprocess.TimeoutExpired("systemd-run", 10), FileNotFoundError("systemd-run")):
            with self.subTest(error=type(error).__name__), mock.patch.dict(os.environ, {"BUILDLOG_SCOPE": "1"}), \
                    mock.patch.object(merge_gate, "in_builds_slice", return_value=False), \
                    mock.patch("scripts.production.merge_gate.subprocess.run", side_effect=error) as probe, \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(merge_gate.suite_scope_prefix(), [])
                self.assertEqual(probe.call_args.kwargs["timeout"], 10)
                self.assertIn("Module scope skipped: probe failed", output.getvalue())

    def test_builds_slice_detection_reads_the_gates_cgroup(self) -> None:
        for cgroup, expected in (("0::/user.slice/builds.slice/run-123.scope\n", True),
                                 ("0::/user.slice/app.slice/session.scope\n", False)):
            with self.subTest(cgroup=cgroup), mock.patch.object(Path, "read_text", autospec=True, return_value=cgroup) as read:
                self.assertEqual(merge_gate.in_builds_slice(), expected)
                read.assert_called_once_with(Path("/proc/self/cgroup"))

    def test_more_than_the_rerun_limit_keeps_all_first_failures(self) -> None:
        failed_suites = EXPECTED_SUITES[:merge_gate.RERUN_LIMIT]
        for suite in failed_suites:
            self.write(self.state / ("red-" + suite), "red")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"{merge_gate.RERUN_LIMIT + 1} modules failed; this is a real breakage, not load", result.stdout)
        self.assertNotIn("Rerun alone:", result.stdout)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), MODULES)

    def test_a_module_that_grows_past_the_cap_is_killed_and_the_gate_is_red(self) -> None:
        self.write(self.state / "grow-memory", "grow")
        result = self.run_gate("--tests-only", "--memory-cap-mib", "256")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"(?m)^hooks/test_first\.py killed: sampled memory [\d.]+ GiB is over the 0\.250 GiB cap$")
        self.assertRegex(result.stdout, r"(?m)^hooks: RED ")
        self.assertNotIn("never killed", result.stdout)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), MODULES)

    def test_a_module_still_running_at_the_time_limit_is_killed_and_the_gate_is_red(self) -> None:
        self.write(self.state / "hold-module", "hold")
        self.environment["BUILDLOG_SCOPE"] = "1"
        output = io.StringIO()
        monotonic = time.monotonic
        deadline = monotonic() + 300
        sampled: set[int] = set()
        killed: list[int] = []
        kill = merge_gate.kill_job_group

        def module_clock() -> float:
            if monotonic() >= deadline:
                raise TimeoutError("gate did not stop the held module")
            ready = self.state / "module-held"
            return monotonic() + (2000 if ready.exists() and int(ready.read_text()) in sampled else 0)

        def sample(roots: set[int]) -> dict[int, float]:
            sampled.update(roots)
            return dict.fromkeys(roots, 1.0)

        def kill_module(job: merge_gate.RunningGateJob) -> None:
            killed.append(job.process.pid)
            kill(job)

        with mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "TEST_SUITES", ("hooks",)), \
                mock.patch.object(merge_gate, "in_builds_slice", return_value=False), \
                mock.patch("scripts.production.merge_gate.time.monotonic", side_effect=module_clock), \
                mock.patch.object(merge_gate, "process_memory_mib", side_effect=sample), \
                mock.patch.object(merge_gate, "kill_job_group", side_effect=kill_module), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout)
        self.assertFalse(result.passed, output.getvalue())
        self.assertIn("hooks/test_first.py killed: still running after 1800 s, the module time limit", output.getvalue())
        self.assertRegex(output.getvalue(), r"(?m)^hooks: RED ")
        held = int((self.state / "module-held").read_text())
        self.assert_process_stopped(held)
        self.assertIn(held, sampled)
        self.assertEqual(set(killed), {held})
        scopes = [call for call in self.calls() if call["tool"] == "systemd-run" and call["args"][-1] != "true"]
        self.assertEqual([call["pid"] for call in scopes], [held])
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertEqual(len(starts), 1)

    def test_nonpositive_module_time_limit_refuses_without_running_modules(self) -> None:
        result = self.run_gate("--tests-only", "--module-time-limit-s", "0")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("module time limit must be positive", result.stdout + result.stderr)
        self.assertEqual(self.module_events(), [])

    def test_a_sample_counts_swapped_memory(self) -> None:
        with mock.patch.object(merge_gate, "swapped_kib", return_value=1024 * 1024):
            sample = merge_gate.process_memory_mib({os.getpid()})
        self.assertGreater(sample[os.getpid()], 1024.0)

    def test_the_type_check_venv_is_built_once_with_the_pinned_stubs(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            first, second = merge_gate.typecheck_python(), merge_gate.typecheck_python()
        venv = self.state / "runner" / "typecheck-venv"
        self.assertEqual((first, second), (venv / "bin" / "python", venv / "bin" / "python"))
        self.assertEqual([call["args"] for call in self.calls() if call["tool"] == "uv"],
                         [["venv", "--clear", str(venv)],
                          ["pip", "install", "--python", str(venv / "bin" / "python"),
                           *merge_gate.TYPECHECK_REQUIREMENTS]])

    def test_a_type_check_venv_that_cannot_be_built_is_red_and_names_the_command(self) -> None:
        self.write(self.state / "uv-fails", "fail")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("basedpyright: RED\ntype-check venv missing: fixture: no network; build it with: ", result.stdout)
        self.assertIn(" pip install --python ", result.stdout)
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        self.assertRegex(result.stdout, r"(?m)^lib: OK \d+\.\d+s$")
        self.assert_not_pushed()

    def test_type_failure_resets_merge_and_does_not_push(self) -> None:
        self.write(self.state / "types-red", "red")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture type error", result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_red_gate_restores_tracked_debris_before_rollback(self) -> None:
        self.write(self.state / "modify-tracked", "modify")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertIn("merge undone", result.stderr)
        self.assert_not_pushed()

    def test_failed_rollback_names_merge_commit_and_blocking_files(self) -> None:
        with mock.patch.object(merge_gate, "git", return_value=subprocess.CompletedProcess([], 1, " M unit.txt", "blocked")), mock.patch.object(
                merge_gate, "checked_git", side_effect=["merge-commit", "unit.txt\0"]):
            with self.assertRaisesRegex(merge_gate.GateRefused, "merge NOT undone; merge commit merge-commit; blocking files: M unit.txt"):
                merge_gate.rollback_merge(self.checkout, self.base)

    def test_finished_module_leaves_no_running_child(self) -> None:
        self.write(self.state / "leak-child", "leak")
        result = self.run_gate("--tests-only")
        child = int((self.state / "child-pid").read_text())
        try:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_process_stopped(child)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_explicit_fetch_updates_refs_without_matching_remote_configuration(self) -> None:
        _ = self.git(self.checkout, "config", "--unset-all", "remote.origin.fetch")
        _ = self.git(self.checkout, "update-ref", "refs/remotes/origin/unit-phase", self.base)
        result = self.run_gate("--promote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "origin/unit-phase"), self.checkpoint)

    def test_recorded_durations_start_unknown_then_slowest_modules(self) -> None:
        timings = {f"{suite}/test_first.py": 1.0 for suite in EXPECTED_SUITES}
        timings["direct/test_first.py"] = 100.0
        self.write(self.state / "runner" / "module_seconds.json", json.dumps(timings))
        scripts = self.checkout / "scripts"
        modules = [*((suite, scripts / suite / "test_first.py") for suite in EXPECTED_SUITES),
                   ("lib", scripts / "lib" / "test_second.py")]
        ordered = merge_gate.module_order(modules, timings)
        self.assertEqual([(suite, path.name) for suite, path in ordered[:2]],
                         [("lib", "test_second.py"), ("direct", "test_first.py")])
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        updated = cast(object, json.loads((self.state / "runner" / "module_seconds.json").read_text()))
        self.assertIsInstance(updated, dict)
        assert isinstance(updated, dict)
        self.assertEqual(len(cast(dict[str, object], updated)), MODULES)

    def test_termination_signals_roll_back_and_clean_module_groups(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = 0
                (self.state / "module-held").unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                process = subprocess.Popen(
                    [sys.executable, str(SCRIPT), "--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
                     "--unit", "fixture", "--phase", "repair", "--settings-blob", self.settings_blob,
                     "--production", str(self.quiet_production), "--type-dirs", "scripts"], env=self.environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 300
                    while not (self.state / "module-held").exists():
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail("fixture module never became ready")
                        time.sleep(0.01)
                    child = int((self.state / "module-held").read_text())
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=300)
                    self.assertEqual(process.returncode, 1, output + errors)
                    self.assertIn("merge undone", errors)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assertFalse(Path(f"/proc/{child}").exists())
                    self.assert_not_pushed()
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=300)
                    if child:
                        try:
                            os.killpg(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    _ = self.git(self.checkout, "reset", "--keep", self.base)

    def test_signal_during_merge_aborts_pending_merge_and_its_hook(self) -> None:
        ready = self.state / "merge-hook-ready"
        hook = self.checkout / ".git" / "hooks" / "prepare-commit-msg"
        # The pid is published by rename, so the test never reads a created but
        # still empty file.
        self.write(hook, f"#!{sys.executable}\nimport os, time\nfrom pathlib import Path\n"
                   + f"Path({str(ready) + '.tmp'!r}).write_text(str(os.getpid()))\n"
                   + f"os.replace({str(ready) + '.tmp'!r}, {str(ready)!r})\nwhile True: time.sleep(0.01)\n")
        hook.chmod(0o755)
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--repo", str(self.checkout), "--merge-branch", "integration",
             "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
             "--unit", "fixture", "--phase", "repair", "--settings-blob", self.settings_blob,
             "--production", str(self.quiet_production)],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        hook_pid = 0
        try:
            deadline = time.monotonic() + 300
            while not ready.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("merge hook did not become ready")
                time.sleep(0.01)
            hook_pid = int(ready.read_text())
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=300)
            self.assertEqual(process.returncode, 1, output + errors)
            self.assertIn("merge undone", errors)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())
            self.assert_process_stopped(hook_pid)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assert_not_pushed()
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=300)
            if hook_pid:
                try:
                    os.kill(hook_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_sampled_admission_learns_headroom_and_reserves_between_scans(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.observe(100.0, 300.0, {1: 100.0, 2: 30.0})
        self.assertTrue(admission.admits(True))
        admission.reserve(3)
        self.assertFalse(admission.admits(True))
        self.assertTrue(admission.admits(False))
        admission.observe(101.0, 450.0, {1: 80.0})
        self.assertFalse(admission.admits(True))
        self.assertEqual(admission.headroom_mib, 100.0)
        self.assertEqual(admission.peak_mib, 450.0)

    def test_starting_job_keeps_its_reservation_after_a_small_rss_sample(self) -> None:
        admission = merge_gate.SampledRSSAdmission(256)
        admission.observe(100.0, 24.0, {})
        admission.reserve(1)
        admission.reserve(2)
        self.assertFalse(admission.admits(True))
        admission.observe(100.25, 40.0, {1: 8.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 176.0)
        self.assertFalse(admission.admits(True))
        # At the reservation size, real RSS replaces padding without lowering
        # the charged total; a completed job releases its padding.
        admission.observe(100.5, 128.0, {1: 96.0, 2: 8.0})
        self.assertEqual(admission.reserved_mib, 88.0)
        self.assertFalse(admission.admits(True))
        admission.release(2)
        self.assertEqual(admission.reserved_mib, 0.0)
        self.assertTrue(admission.admits(True))

    def test_larger_job_peak_increases_every_running_jobs_reservation(self) -> None:
        admission = merge_gate.SampledRSSAdmission(512)
        admission.observe(100.0, 280.0, {1: 240.0, 2: 16.0})
        self.assertEqual(admission.headroom_mib, 240.0)
        self.assertEqual(admission.reserved_mib, 224.0)
        self.assertFalse(admission.admits(True))

    def test_a_job_reserved_at_a_known_size_holds_it_and_leaves_module_headroom_alone(self) -> None:
        admission = merge_gate.SampledRSSAdmission(4096)
        admission.observe(100.0, 0.0, {})
        admission.reserve(1, 3072.0)
        self.assertEqual(admission.reserved_mib, 3072.0)
        self.assertTrue(admission.admits(True))
        admission.observe(100.25, 2700.0, {1: 2700.0})
        self.assertEqual(admission.headroom_mib, 96.0)
        self.assertEqual(admission.reserved_mib, 372.0)
        self.assertTrue(admission.admits(True))
        admission.release(1)
        self.assertEqual(admission.fixed_mib, {})

    def test_a_module_three_times_slower_than_its_record_and_a_minute_over_is_slow(self) -> None:
        self.assertTrue(merge_gate.slower_than_recorded(1264.0, 49.0))
        self.assertTrue(merge_gate.slower_than_recorded(90.0, 30.0))
        self.assertFalse(merge_gate.slower_than_recorded(89.0, 30.0))
        self.assertFalse(merge_gate.slower_than_recorded(12.0, 1.0))

    def test_the_report_names_a_module_slower_than_its_record(self) -> None:
        runner = self.state / "runner"
        runner.mkdir()
        self.write(runner / "module_history.json", json.dumps({
            "lib/test_second.py": [0.0, 0.0, 1000.0], "hooks/test_first.py": [1000.0] * 5}))
        with mock.patch.object(merge_gate, "SLOW_MODULE_FLOOR_S", 0.0), \
                mock.patch.dict(os.environ, self.environment), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            run = merge_gate.run_suites(self.checkout)
        self.assertTrue(run.passed, output.getvalue())
        self.assertRegex(output.getvalue(), r"(?m)^Slow module: lib/test_second\.py \d+ s, median of the last 3 runs 0 s \(3x or more\)$")
        history = cast(dict[str, list[float]], json.loads((runner / "module_history.json").read_text()))
        self.assertEqual(history["lib/test_second.py"][:3], [0.0, 0.0, 1000.0])
        self.assertEqual(len(history["lib/test_second.py"]), 4)
        self.assertEqual(history["hooks/test_first.py"][:4], [1000.0] * 4)
        self.assertEqual(len(history["hooks/test_first.py"]), 5)
        self.assertNotIn("Slow module: hooks/", output.getvalue())

    def test_memory_scan_schedule_uses_controlled_quarter_second_clock(self) -> None:
        admission = merge_gate.SampledRSSAdmission(3072)
        self.assertTrue(admission.sample_due(10.0))
        admission.observe(10.0, 30.0, {})
        for tick in (10.05, 10.10, 10.15, 10.20):
            self.assertFalse(admission.sample_due(tick))
        self.assertTrue(admission.sample_due(10.25))

    def test_sampled_overshoot_is_red_but_every_module_still_runs(self) -> None:
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "process_memory_mib", return_value={os.getpid(): 100.0}), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout, 48)
        self.assertFalse(result.passed)
        self.assertEqual(len(result.jobs), MODULES)
        self.assertIn("sampled memory exceeded the cap", output.getvalue())

    def test_runner_reports_load_at_its_own_start_and_end(self) -> None:
        output = io.StringIO()
        calls = 0

        def measured_load() -> float:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 10.0
            events = self.state / "modules.jsonl"
            return 20.0 if events.exists() and len(events.read_text().splitlines()) == 2 * MODULES else 60.0

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "one_minute_load", side_effect=measured_load), redirect_stdout(output):
            result = merge_gate.run_suites(self.checkout)
        self.assertTrue(result.passed, output.getvalue())
        self.assertIn("One-minute load: start=10.00 max=60.00 end=20.00", output.getvalue())

    def test_mac_timeout_or_interruption_reports_that_main_was_already_promoted(self) -> None:
        args = merge_gate.GateArguments(main_repo=str(self.main), merge_branch="integration", settings_blob=self.settings_blob,
                                       ssh="fake-ssh", mac_repo="~/fixture", mac_host="fixture")
        for failure in (subprocess.TimeoutExpired("fake-ssh", 25), merge_gate.GateRefused("interrupted by SIGTERM"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), mock.patch.dict(os.environ, self.environment), mock.patch.object(merge_gate, "wait_for_release_lock"), mock.patch.object(
                    merge_gate, "checked_git", side_effect=["main", "", self.base, self.base, self.settings_blob, "", "", self.base]) as checked, mock.patch.object(
                    merge_gate, "git", return_value=subprocess.CompletedProcess([], 0, "", "")), mock.patch.object(
                    subprocess, "run", side_effect=failure):
                with self.assertRaisesRegex(merge_gate.GateRefused, f"main promoted to {self.base}; Mac fast-forward failed"):
                    merge_gate.promote(args)
            self.assertIn(mock.call(self.main, "push", "origin", "main"), checked.call_args_list)

    def test_settings_filter_rejects_changed_blob(self) -> None:
        self.write(self.unit / "settings.json", '{"unexpected": true}\n')
        self.checkpoint = self.commit(self.unit, "unapproved settings")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("settings", (result.stdout + result.stderr).lower())
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_promote_fast_forwards_scratch_main_and_checks_mac_result(self) -> None:
        result = self.run_gate("--promote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), head)
        ssh_calls = [call for call in self.calls() if call["tool"] == "ssh"]
        self.assertEqual(len(ssh_calls), 1)
        self.assertIn("--ff-only", " ".join(ssh_calls[0]["args"]))
        self.assertIn("ff_rc=", " ".join(ssh_calls[0]["args"]))

    def test_promote_only_promotes_the_pushed_merge_branch_without_a_gate(self) -> None:
        self.assertEqual(self.run_gate("--promote").returncode, 0)
        result = self.run_promote_only()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "main"))
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 2)

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
            _ = process.communicate(timeout=300)

    def test_finished_release_and_live_command_text_do_not_block_promotion(self) -> None:
        args = self.publish_for_promotion()
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        holders.mkdir()
        cycle_id = "a" * 32
        instant = "2026-10-28T14:50:00-04:00"
        self.write(release / cycle_id / "cycle.json", json.dumps({
            "id": cycle_id, "opened_at": instant, "release_started_at": instant,
            "holders": {"fixture": {"since": instant, "released_at": instant}},
            "recipients": {}, "entries": []}))
        self.write(release / "current", cycle_id)
        self.write(release / "release.lock", "")
        ready = self.state / "command-text-ready"
        source = ("import pathlib, signal, sys; signal.signal(signal.SIGTERM, signal.SIG_DFL); "
                  "pathlib.Path(sys.argv[1]).touch(); sys.stdin.readline()")
        process = subprocess.Popen(
            [sys.executable, "-c", source, str(ready), "build_hold.py", "release"],
            env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True)
        try:
            wait_for_file(ready)
            with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
                merge_gate.promote(args)
            self.assertIsNone(process.poll())
            self.assertEqual(list(holders.iterdir()), [])
            self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
            self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=300)

    def test_promotion_waits_for_another_process_to_free_the_release_lock(self) -> None:
        args = self.publish_for_promotion()
        with self.release_lock_held_by_child() as process:
            def release_during_wait(_seconds: float) -> None:
                self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
                self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))
                _, errors = process.communicate(input="release\n", timeout=300)
                self.assertEqual(process.returncode, 0, errors)

            pause = mock.Mock(side_effect=release_during_wait)
            clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[10.0, 10.0, 11.0]), sleep=pause)
            with mock.patch.dict(os.environ, self.environment), \
                    mock.patch.object(merge_gate, "time", clock), redirect_stdout(io.StringIO()):
                merge_gate.promote(args, release_lock_wait_s=0.5, release_lock_poll_s=0.01)
            pause.assert_called_once_with(0.01)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.checkpoint)

    def test_promotion_refuses_when_the_release_lock_stays_held_for_the_whole_wait(self) -> None:
        args = self.publish_for_promotion()
        pause = mock.Mock()
        clock = SimpleNamespace(monotonic=mock.Mock(side_effect=[10.0, 10.0, 70.0]), sleep=pause)
        with self.release_lock_held_by_child(), mock.patch.dict(os.environ, self.environment), \
                mock.patch.object(merge_gate, "time", clock):
            with self.assertRaisesRegex(merge_gate.GateRefused,
                    "^build-hold state stayed locked for 60 s; main promotion refused$"):
                merge_gate.promote(args)
            pause.assert_called_once_with(0.2)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promotion_does_not_create_a_missing_release_directory_or_lock(self) -> None:
        args = self.publish_for_promotion()
        release = Path(self.environment["BUILD_HOLD_RELEASE_DIR"])
        with mock.patch.dict(os.environ, self.environment), redirect_stdout(io.StringIO()):
            merge_gate.promote(args)
            self.assertFalse(release.exists())
            release.mkdir()
            merge_gate.promote(args)
            self.assertEqual(list(release.iterdir()), [])
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), self.checkpoint)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 2)

    def test_promote_preserves_unrelated_local_edits_and_untracked_files(self) -> None:
        self.write(self.main / "subject.txt", "uncommitted main\n")
        self.write(self.main / "accounts.lock", "untracked\n")
        result = self.run_gate("--promote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.main / "subject.txt").read_text(), "uncommitted main\n")
        self.assertEqual((self.main / "accounts.lock").read_text(), "untracked\n")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.git(self.checkout, "rev-parse", "HEAD"))

    def test_promote_preserves_local_edits_that_block_fast_forward(self) -> None:
        self.write(self.unit / "subject.txt", "checkpoint changes this file\n")
        self.checkpoint = self.commit(self.unit, "touch subject")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.write(self.main / "subject.txt", "uncommitted main\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.main / "subject.txt").read_text(), "uncommitted main\n")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_divergent_main(self) -> None:
        self.write(self.main / "main-only.txt", "divergent\n")
        main_head = self.commit(self.main, "main divergence")
        _ = self.git(self.main, "push", "origin", "main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), main_head)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_unpublished_main_commits(self) -> None:
        self.write(self.main / "main-only.txt", "unpublished\n")
        main_head = self.commit(self.main, "unpublished main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_promote_refuses_checkout_on_another_branch(self) -> None:
        _ = self.git(self.main, "checkout", "-b", "other-main")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.main, "branch", "--show-current"), "other-main")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_mac_nonzero_ff_rc_is_failure_even_when_ssh_exits_zero(self) -> None:
        self.write(self.state / "mac-answer", "ff_rc=1\nfixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ff_rc=1", result.stdout + result.stderr)
        head = self.git(self.origin, "rev-parse", "main")
        self.assertIn(f"main promoted to {head}; Mac fast-forward failed", result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_mac_missing_ff_rc_is_failure(self) -> None:
        self.write(self.state / "mac-answer", "fixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)

    def test_mac_conflicting_ff_rc_lines_are_failure(self) -> None:
        self.write(self.state / "mac-answer", "ff_rc=0\nff_rc=1\nfixture-head\n")
        result = self.run_gate("--promote")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "ssh"]), 1)


@final
class MergeGateHoldTests(TemporaryHoldTest):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.gate = MergeGateTests()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self.gate.doCleanups)
        self.gate.setUp()
        # Keep the hold helper's roots and owner, and the gate helper's tools and repos.
        self.environment = {**self.gate.environment, **{
            name: os.environ[name] for name in (
                "BUILD_HOLD_DIR", "BUILD_HOLD_RELEASE_DIR", "BUILD_TARGETS_CONFIG",
                "MAC_TEST_STATE_DIR", "NOTIFIER_SESSIONS_DIR", "CLAUDE_CODE_SESSION_ID",
                "UNIT_ACTIVE_DIR", "UNIT_RUN_DIR", "UNIT_SESSION_DIR",
                "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_SESSION_DIR",
            )
        }}
        self.enterContext(mock.patch.dict(os.environ, self.environment))

    def record(self, event: str) -> None:
        with (self.gate.state / "modules.jsonl").open("a") as output:
            fcntl.flock(output, fcntl.LOCK_EX)
            _ = output.write(json.dumps({"suite": "", "module": "", "pid": os.getpid(),
                                         "event": event, "tmpdir": ""}) + "\n")

    def test_each_mode_waits_before_tests_types_or_promotion(self) -> None:
        arguments = ["--repo", str(self.gate.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.gate.checkpoint,
                     "--previous-checkpoint", self.gate.base, "--unit", "fixture-unit",
                     "--phase", "hold", "--type-dirs", "scripts/production",
                     "--production", str(self.gate.quiet_production),
                     "--settings-blob", self.gate.settings_blob,
                     "--main-repo", str(self.gate.main), "--ssh", str(self.gate.bin / "ssh")]
        for mode in ([], ["--tests-only"], ["--promote-only"]):
            with self.subTest(mode=mode):
                os.environ["MERGE_GATE_PASSED_DIR"] = str(self.gate.state / ("passes-" + "-".join(mode)))
                (self.gate.state / "modules.jsonl").unlink(missing_ok=True)
                (self.gate.state / "tools.jsonl").unlink(missing_ok=True)
                self.hold()
                before_merge = self.gate.git(self.gate.checkout, "rev-parse", "HEAD")
                before_main = self.gate.git(self.gate.main, "rev-parse", "HEAD")

                def release(_seconds: float) -> None:
                    self.assertEqual(self.gate.module_events(), [])
                    self.assertEqual(self.gate.calls(), [])
                    self.assertEqual(self.gate.git(self.gate.checkout, "rev-parse", "HEAD"), before_merge)
                    self.assertEqual(self.gate.git(self.gate.main, "rev-parse", "HEAD"), before_main)
                    self.record("release")
                    (self.holders / "other").unlink()

                events = IntervalReleasePolling()
                output = io.StringIO()
                with mock.patch.object(merge_gate, "local_release_waiter", return_value=events), \
                        mock.patch.object(events, "wait", side_effect=release), \
                        redirect_stdout(output):
                    code = merge_gate.main([*arguments, *mode])
                self.assertEqual(code, 0, output.getvalue())
                self.assertEqual(output.getvalue().count("natedev is held by builder; waiting"), 1)
                self.assertEqual(output.getvalue().count("Gate hold: released after"), 1)
                recorded = self.gate.module_events()
                self.assertEqual(recorded[0]["event"], "release")
                if mode == ["--promote-only"]:
                    self.assertEqual(len(recorded), 1)
                    self.assertEqual(len([call for call in self.gate.calls() if call["tool"] == "ssh"]), 1)
                else:
                    self.assertEqual(len([event for event in recorded if event["event"] == "finish"]), MODULES)

    def test_sole_holder_runs_all_modules_and_types_without_waiting(self) -> None:
        self.hold("caller", "builder")
        output = io.StringIO()
        with mock.patch.object(merge_gate, "local_release_waiter",
                               side_effect=AssertionError("the sole holder must run")), redirect_stdout(output):
            run = merge_gate.run_suites(self.gate.checkout, type_dirs=("scripts/production",))
        self.assertTrue(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), MODULES + 1)
        self.assertTrue((self.holders / "caller").exists())
        self.assertTrue((self.gate.state / "types-ran").exists())
        self.assertNotIn("Gate hold:", output.getvalue())
        self.assertNotIn("Gate hold wait:", output.getvalue())

    def test_mid_gate_hold_stops_new_starts_and_excludes_wait_time(self) -> None:
        self.gate.write(self.gate.state / "concurrency-barrier", "held")
        start = merge_gate.start_gate_job
        finish = merge_gate.finish_gate_job
        started: list[merge_gate.RunningGateJob] = []
        now = [0.0]
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep)

        def start_module(suite: str, path: Path, argv: list[str], repo: Path,
                         environment: dict[str, str], log: Path) -> merge_gate.RunningGateJob:
            self.assertFalse((self.holders / "other").exists(), self.gate.module_events())
            job = start(suite, path, argv, repo, environment, log)
            started.append(job)
            wait_for_file(self.gate.state / f"{job.process.pid}.started")
            if len(started) == 1:
                self.hold()
                self.record("hold")
            (self.gate.state / f"{job.process.pid}.released").touch()
            return job

        def finish_module(job: merge_gate.RunningGateJob, status: int) -> merge_gate.GateJobResult:
            result = finish(job, status)
            if job is started[0]:
                recorded = self.gate.module_events()
                self.assertEqual([event["event"] for event in recorded], ["start", "hold", "finish"])
                now[0] += 37.0
                self.record("release")
                (self.holders / "other").unlink()
            return result

        output = io.StringIO()
        with mock.patch.object(merge_gate, "start_gate_job", side_effect=start_module), \
                mock.patch.object(merge_gate, "finish_gate_job", side_effect=finish_module), \
                mock.patch.object(merge_gate, "time", clock), redirect_stdout(output):
            run = merge_gate.run_suites(self.gate.checkout, max_running_jobs=2)
        self.assertTrue(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), MODULES)
        self.assertEqual(len([event for event in self.gate.module_events() if event["event"] == "finish"]), MODULES)
        self.assertEqual(run.seconds, 0.0)
        self.assertIn("Gate hold wait: 37.00s (excluded from Gate tests)", output.getvalue())
        self.assertIn("Gate tests: 0.00s;", output.getvalue())

    def test_final_hold_read_error_undoes_the_merge_without_pushing(self) -> None:
        check_settings = merge_gate.check_settings

        def fail_final_hold_read(repo: Path, revision: str, approved: str) -> None:
            check_settings(repo, revision, approved)
            self.assertNotEqual(self.gate.git(repo, "rev-parse", "HEAD"), self.gate.base)
            self.unreadable()

        output = io.StringIO()
        with mock.patch.object(merge_gate, "check_settings", side_effect=fail_final_hold_read), \
                contextlib.redirect_stderr(output), redirect_stdout(output):
            code = merge_gate.main([
                "--repo", str(self.gate.checkout), "--merge-branch", "integration",
                "--unit-branch", "unit-phase", "--checkpoint", self.gate.checkpoint,
                "--previous-checkpoint", self.gate.base, "--unit", "fixture-unit", "--phase", "hold",
                "--production", str(self.gate.quiet_production), "--type-dirs", "scripts/production",
                "--settings-blob", self.gate.settings_blob,
            ])
        self.assertNotEqual(code, 0, output.getvalue())
        self.assertIn("natedev hold state cannot be read", output.getvalue())
        self.assertIn("merge undone, not pushed", output.getvalue())
        self.assertEqual(self.gate.git(self.gate.checkout, "rev-parse", "HEAD"), self.gate.base)
        self.gate.assert_not_pushed()
        self.assertFalse((self.gate.checkout / "unit.txt").exists())

    def test_signalled_module_and_types_jobs_resume_after_hold_release(self) -> None:
        for suite in ("hooks", "types"):
            with self.subTest(suite=suite):
                os.environ["MERGE_GATE_PASSED_DIR"] = str(self.gate.state / ("passes-" + suite))
                (self.gate.state / "modules.jsonl").unlink(missing_ok=True)
                self.gate.write(self.gate.state / "signal-hooks", "once")
                if suite == "types":
                    self.gate.write(self.gate.bin / "basedpyright", "#!" + sys.executable + "\n"
                                    + "import os, pathlib, signal\n"
                                    + "marker = pathlib.Path(os.environ['MERGE_GATE_TEST_STATE']) / 'signal-types'\n"
                                    + "if marker.exists():\n"
                                    + "    marker.unlink()\n"
                                    + "    os.kill(os.getpid(), signal.SIGTERM)\n"
                                    + "print('0 errors, 0 warnings, 0 notes')\n")
                    self.gate.write(self.gate.state / "signal-types", "once")
                    (self.gate.state / "signal-hooks").unlink()
                start = merge_gate.start_gate_job
                starts: list[str] = []

                def start_job(name: str, path: Path, argv: list[str], repo: Path,
                              environment: dict[str, str], log: Path) -> merge_gate.RunningGateJob:
                    self.assertFalse((self.holders / "other").exists())
                    if not starts:
                        self.hold()
                        self.record("hold")
                    starts.append(name)
                    return start(name, path, argv, repo, environment, log)

                def release(_seconds: float) -> None:
                    self.assertEqual(starts, [suite])
                    self.record("release")
                    (self.holders / "other").unlink()

                events = IntervalReleasePolling()
                output = io.StringIO()
                with mock.patch.object(merge_gate, "TEST_SUITES", ("hooks",)), \
                        mock.patch.object(merge_gate, "RERUN_LIMIT", 0), \
                        mock.patch.object(merge_gate, "start_gate_job", side_effect=start_job), \
                        mock.patch.object(merge_gate, "local_release_waiter", return_value=events), \
                        mock.patch.object(events, "wait", side_effect=release), redirect_stdout(output):
                    run = merge_gate.run_suites(self.gate.checkout, max_running_jobs=1,
                                               type_dirs=("scripts/production",) if suite == "types" else ())
                self.assertTrue(run.passed, output.getvalue())
                self.assertEqual(starts[:2], [suite, suite])
                self.assertEqual(len(run.jobs), 2 if suite == "types" else 1)
                self.assertIn("stopped during a natedev hold; queued again", output.getvalue())
                self.assertNotIn("Rerun alone:", output.getvalue())
                events_recorded = [event["event"] for event in self.gate.module_events()]
                self.assertLess(events_recorded.index("hold"), events_recorded.index("release"))
                self.assertLess(events_recorded.index("release"), events_recorded.index("finish"))

    def test_a_signal_without_a_hold_keeps_the_module_red(self) -> None:
        self.gate.write(self.gate.state / "signal-hooks", "always")
        output = io.StringIO()
        with mock.patch.object(merge_gate, "TEST_SUITES", ("hooks",)), redirect_stdout(output):
            run = merge_gate.run_suites(self.gate.checkout)
        self.assertFalse(run.passed, output.getvalue())
        self.assertEqual(len(run.jobs), 1)
        self.assertEqual(run.jobs[0].returncode, -signal.SIGTERM)
        self.assertEqual([event["event"] for event in self.gate.module_events()], ["start", "start"])
        self.assertNotIn("queued again", output.getvalue())

    def test_unreadable_hold_state_refuses_before_any_job(self) -> None:
        self.unreadable()
        with self.assertRaisesRegex(merge_gate.GateRefused, "natedev hold state cannot be read"):
            _ = merge_gate.run_suites(self.gate.checkout, type_dirs=("scripts/production",))
        self.assertEqual(self.gate.calls(), [])
        self.assertEqual(self.gate.module_events(), [])


if __name__ == "__main__":
    _ = unittest.main()
