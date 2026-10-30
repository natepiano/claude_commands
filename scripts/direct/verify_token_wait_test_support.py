"""Independent tools, state and process cleanup for verify token tests."""
from __future__ import annotations

import json
import os
import signal
import shutil
import sys
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar, cast, override
from scripts.direct.wait_for_test_state import wait_until
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.local_release_wait import LocalReleaseEvents, local_release_waiter
from scripts.direct.verify_mac_offload_test_support import RUSTC_STUB, stub_build_tool_path
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


VERIFY = Path(__file__).with_name("verify.sh")


BOARD = VERIFY.with_name("board.sh")



Record = dict[str, object]


def restore_sigint() -> None:
    _ = signal.signal(signal.SIGINT, signal.SIG_DFL)



class VerifyTokenWaitFixture(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    started_groups: set[int] = set()

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="verify-token-")
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture_root = Path(temporary.name) / "case"
        cls.fixture_snapshot = Path(temporary.name) / "seed"
        cls.fixture_root.mkdir()
        builder = cls()
        builder.root = cls.fixture_root
        builder.build_fixture()
        cls.fixture_environment = builder.environment.copy()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.started_groups = set()
        self.root = self.fixture_root
        _ = shutil.copytree(self.fixture_snapshot, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.environment = self.fixture_environment.copy()

    def build_fixture(self) -> None:
        home = self.root / "home"
        home.mkdir()
        (home / ".claude").symlink_to(VERIFY.parent.parent.parent, target_is_directory=True)
        stubs = self.root / "stubs"
        stubs.mkdir()
        cargo = stubs / "cargo"
        _ = cargo.write_text(r'''#!/bin/sh
if [ "$1" = metadata ]; then
    printf "metadata\n" >> "$METADATA_CALLS"
    printf '%s\n' '{"packages":[{"name":"sample","targets":[{"name":"sample","kind":["lib"],"test":true}]}]}'
    exit 0
fi
printf "%s\n" "$*" >> "$CARGO_CALLS"
if [ "$1" = check ]; then
    if [ "${TEST_REQUIRE_OWNED:-0}" = 1 ]; then
        python3 - "$TEST_OWNED_RECORD" "$$" "$TEST_CALLER_GROUP" <<'PY'
import json
import os
from pathlib import Path
import sys

record = json.loads(Path(sys.argv[1]).read_text())
assert record["group"] == os.getpgid(int(sys.argv[2]))
assert record["group"] != int(sys.argv[3])
PY
        [ "$?" = 0 ] || exit 99
        : > "$TEST_OWNED_CONFIRMED"
    fi
    if [ "${TEST_BLOCK:-0}" = 1 ]; then
        printf '%s\n' "$$" > "$TEST_CARGO_PID"
        : > "$TEST_RUNNING"
        python3 "$TEST_WAIT_HELPER" "$TEST_RELEASE"
    fi
    if [ "${TEST_RECLAIM_CHECK:-0}" = 1 ]; then
        old=$(cat "$TEST_CARGO_PID")
        state=$(ps -p "$old" -o stat= 2>/dev/null || true)
        case "$state" in ''|Z*) : ;; *) exit 98 ;; esac
    fi
    if [ "${TEST_CORRUPT_OWNER:-0}" = 1 ]; then
        python3 - "$TEST_OWNED_RECORD" "$TEST_STEP_GROUP" <<'PY'
from pathlib import Path
import json
import sys

record = Path(sys.argv[1])
Path(sys.argv[2]).write_text(str(json.loads(record.read_text())["group"]))
record.write_text("invalid ownership record")
PY
    fi
    if [ -n "${TEST_CARGO_STATUS:-}" ]; then
        exit "$TEST_CARGO_STATUS"
    fi
    exit 0
fi
exit 97
''')
        cargo.chmod(0o755)
        rustc = stubs / "rustc"
        _ = rustc.write_text(RUSTC_STUB)
        rustc.chmod(0o755)
        git = stubs / "git"
        _ = git.write_text(r'''#!/bin/sh
case " $* " in
    *"rev-parse --show-toplevel"*) printf "%s\n" "$PACKAGE_ROOT" ;;
    *"rev-parse HEAD^{tree}"*) printf "fixed-tree\n" ;;
    *"status --porcelain"*|*" ls-tree "*) : ;;
    *) exit 1 ;;
esac
''')
        git.chmod(0o755)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text('lint_config_enabled() { [ "$1" != sweep ]; }\n'
                              + 'lint_config_skip_notice() { :; }\n')
        for name in ("tmp", "runtime", "state", "cache", "session", "target", "cgroup"):
            (self.root / name).mkdir()
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        source = self.root / "src" / "lib.rs"
        source.parent.mkdir()
        _ = source.write_text("pub fn value() -> u8 { 1 }\n")
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
        # Admission must use this test's memory state, including pressure.
        pressure = self.root / "memory-pressure"
        _ = pressure.write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        # Interpreter selection is independent of token ownership. Seed the
        # shim's normal cache once rather than rediscovering Python per test.
        interpreter = self.root / "cache" / "claude" / "python3-3.11"
        interpreter.parent.mkdir()
        _ = interpreter.write_text(sys.executable + "\n")
        memory_event = self.root / "memory-event.py"
        _ = memory_event.write_text(
            "import os, sys\nfrom pathlib import Path\n"
            + f"sys.path.insert(0, {str(VERIFY.parents[2])!r})\n"
            + "from scripts.direct.wait_for_test_state import wait_until\n"
            + "path = Path(os.environ['BUILDLOG_MEMINFO'])\n"
            + "wait_until(lambda: len(values := path.read_text().split()) > 1 and values[1] != '0', path.parent, "
            + f"timeout={TEST_HANG_GUARD_SECS})\n")
        # Memory changes are events. Token retry tests may replace this stub
        # with their own acquisition marker or clock advancement.
        _ = (stubs / "sleep").write_text(
            '#!/bin/sh\n'
            + 'if [ "$1" = "${BUILDLOG_MEM_POLL_S:-5}" ]; then\n'
            + '    read -r label available unit < "$BUILDLOG_MEMINFO"\n'
            + '    if [ "$available" = 0 ]; then exec python3 "$TEST_MEMORY_EVENT"; fi\n'
            + 'fi\nexit 0\n')
        (stubs / "sleep").chmod(0o755)
        self.environment = {
            **{name: value for name, value in os.environ.items()
               if name not in {"UNIT_RUN_DIR", "UNIT_TEAM_ROLE", "CODEX_THREAD_ID",
                               "CODEX_SESSION_ID", "UNIT_DETACHED_RUN", "BASH_ENV", "ENV"}
               and not name.startswith(("PLAN_DELEGATE_", "BASH_FUNC_"))},
            "PATH": stub_build_tool_path(stubs),
            "HOME": str(home),
            # These tests supply the complete build toolchain; nix discovery is
            # covered separately and need not spawn its two interpreters here.
            "IN_NIX_SHELL": "pure",
            "CLAUDE_PYTHON": "",
            "RUSTUP_HOME": str(self.root / "rustup"),
            "PACKAGE_ROOT": str(self.root),
            "CARGO_CALLS": str(self.root / "cargo-calls"),
            "METADATA_CALLS": str(self.root / "metadata-calls"),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "VERIFY_STEPS_DIR": str(self.root / "verify-steps"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_MEMINFO": str(meminfo),
            # Two 12 GiB fallback promises fit this 48 GiB fixture. The host's
            # 21 GiB budget would add an unrelated 120-second ramp wait before
            # the test can observe the token handoff.
            "BUILDLOG_BUILD_BUDGET_GIB": "48",
            "BUILDLOG_MEMORY_PRESSURE": str(pressure),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "hold-release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILD_TARGETS_SCRIPT": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_ADMISSION_FILE": "",
            "BUILDLOG_HOLD_STARTED_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(self.root / "registration"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),
            "VERIFY_MAC_RUNNER": str(self.root / "no-mac-runner"),
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader),
            "UNIT_SESSION_DIR": str(self.root / "session"),
            "TEST_RUNNING": str(self.root / "running"),
            "TEST_RELEASE": str(self.root / "release"),
            "TEST_WAIT_HELPER": str(VERIFY.parent / "wait_for_test_state.py"),
            "TEST_CARGO_PID": str(self.root / "cargo-pid"),
            "TEST_MEMORY_EVENT": str(memory_event),
        }

    @override
    def tearDown(self) -> None:
        for group in self.started_groups:
            self.end_group(group)
            self.assertEqual(self.live_group_processes(group), [], f"group {group} still runs")

    def live_group_processes(self, group: int) -> list[int]:
        """The live processes in the group, and in the session a started call leads.

        verify.sh runs each step in a group of its own (run()'s `set -m`), in the
        call's session; a kill of the call's group alone left a step waiting for the
        token for 45 h (2026-10-20).
        """
        found: list[int] = []
        result = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
                                capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[2].startswith("Z"):
                continue
            pid = int(parts[0])
            if int(parts[1]) == group or self.session_of(pid) == group:
                found.append(pid)
        return found

    @staticmethod
    def session_of(pid: int) -> int | None:
        try:
            return os.getsid(pid)
        except OSError:
            return None

    def end_group(self, group: int) -> None:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while pids := self.live_group_processes(group):
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            descriptors: list[int] = []
            poll_pids = False
            try:
                for pid in pids:
                    try:
                        descriptors.append(os.pidfd_open(pid))
                    except ProcessLookupError:
                        pass
                    except AttributeError:
                        poll_pids = True
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, f"group {group} never stopped")
                if poll_pids:
                    time.sleep(min(0.01, remaining))
                elif descriptors:
                    readable, _, _ = select.select(descriptors, [], [], remaining)
                    self.assertTrue(readable, f"group {group} never stopped")
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

    def record_phase(self, session: Path) -> None:
        self.environment.update({"UNIT_HISTORY_DIR": str(self.root / "history"),
                                 "UNIT_PHASE_ID": "6", "UNIT_SYNC_WRITES": "0",
                                 "UNIT_PASS_OWNER": "launcher", "CODEX_THREAD_ID": "thread-impl",
                                 "CLAUDE_PROBE_SESSION": ""})
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.root),
             "--main-family", "codex", "--main-model", "fixture", "--main-effort", "high",
             "--main-session-id", "fixture-director"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "6", "--phase-title", "Resources"],
        ):
            result = invoke_recorder(arguments, self.environment, self.root)
            self.assertEqual(result.returncode, 0, result.stderr)
        for slot in ("impl", "first", "second"):
            arguments = ["start-pass", "--session-dir", str(session), "--phase-id", "6", "--pass-kind", "impl",
                         "--activity", "verify", "--called-task", "direct.impl", "--called-family", "codex",
                         "--called-model", "fixture", "--worker-name", f"fixture-{slot}"]
            result = invoke_recorder(arguments, {**self.environment, "UNIT_TEAM_ROLE": slot}, self.root)
            self.assertEqual(result.returncode, 0, result.stderr)

        _ = (session / "mesh_roster.json").write_text(json.dumps({
            f"fixture-{slot}": {"thread_id": f"thread-{slot}"} for slot in ("impl", "first", "second")}))

    def phase_waits(self, session: Path) -> list[dict[str, object]]:
        return [row for row in (cast(dict[str, object], json.loads(line)) for line in
                (self.root / "history" / "runs" / f"{session.name}.jsonl").read_text().splitlines())
                if str(row["event_type"]).startswith("wait_")]

    def records(self) -> list[Record]:
        return [record for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()
                if (record := cast(Record, json.loads(line))).get("kind") == "call"]

    def verify(self, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                              env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def wait_for_running(self, path: Path, process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
        wait_until(lambda: path.exists() or process.poll() is not None, path.parent, processes=(process,))
        self.assertTrue(path.exists(), "verify never reached its expected state")

    def start_memory_waiter(self, environment: dict[str, str], errors: Path) -> subprocess.Popen[bytes]:
        """Start a verify that takes the token and then waits on the held meminfo.

        verify.sh tees its step output to stdout, so both streams go to `errors`.
        """
        with errors.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                       cwd=self.root, env=environment, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        self.wait_for_output(errors, "waiting for memory", process)
        self.assertIsNone(process.poll(), errors.read_text(errors="replace"))
        return process

    def wait_for_output(self, path: Path, text: str, process: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        with local_release_waiter(path.parent, path.parent) as events:
            events.arm()
            if isinstance(events, LocalReleaseEvents):
                # Include MODIFY: the child keeps this log open while blocked.
                _ = events.add_watch(events.descriptor, os.fsencode(path.parent), 0x00000FCA)
            while text not in path.read_text(errors="replace") and process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                events.wait(remaining if isinstance(events, LocalReleaseEvents) else min(0.01, remaining))
        self.assertIn(text, path.read_text(errors="replace"))

    def build_stub(self, targets: list[dict[str, object]]) -> dict[str, str]:
        """A cargo whose metadata lists `targets` for sample and whose build makes each bin and example."""
        metadata = json.dumps({"packages": [{"name": "sample", "targets": targets, "features": {}}],
                               "target_directory": str(self.root / "target")})
        cargo = self.root / "stubs" / "cargo"
        _ = cargo.write_text("#!/bin/sh\n"
                             + 'if [ "$1" = metadata ]; then printf "%s\\n" "$TEST_METADATA"; exit 0; fi\n'
                             + 'printf "%s\\n" "$*" >> "$CARGO_CALLS"\n'
                             + 'profile=debug; kind=""\n'
                             + 'for word in "$@"; do case "$word" in --release) profile=release ;; esac; done\n'
                             + 'for word in "$@"; do\n'
                             + '    case "$kind" in\n'
                             + '        bin) mkdir -p "$CARGO_TARGET_DIR/$profile"; : > "$CARGO_TARGET_DIR/$profile/$word"; chmod +x "$CARGO_TARGET_DIR/$profile/$word" ;;\n'
                             + '        example) mkdir -p "$CARGO_TARGET_DIR/$profile/examples"; : > "$CARGO_TARGET_DIR/$profile/examples/$word"; chmod +x "$CARGO_TARGET_DIR/$profile/examples/$word" ;;\n'
                             + '    esac\n'
                             + '    case "$word" in --bin) kind=bin ;; --example) kind=example ;; *) kind="" ;; esac\n'
                             + 'done\n')
        return {**self.environment, "TEST_METADATA": metadata,
                "PLAN_DELEGATE_BOARD_DIR": str(self.root / "board"), "PLAN_DELEGATE_TEAM_ROLE": "impl"}

    def build(self, environment: dict[str, str], *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), "build", "sample", *words], cwd=self.root, env=environment,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def hold_environment(self, board: Path, wait_status: int) -> dict[str, str]:
        """A held natedev for the step's hold wait only.

        verify.sh's first wait-local, before it takes the token, returns at once;
        the step's wait-local marks `hold-waiting`, then waits for `hold-release`
        and exits with `wait_status`. Every other verb succeeds.
        """
        holds = self.root / "build-hold"
        holds.mkdir(exist_ok=True)
        _ = (holds / "holder.json").write_text("{}")
        stub = self.root / "build_targets_stub.py"
        _ = stub.write_text(r"""import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["TEST_REPO"])
from scripts.direct.wait_for_test_state import wait_until
root = Path(os.environ["TEST_HOLD_ROOT"])
if sys.argv[1] == "eligible":
    print("natedev_only test target")
    print("natedev\teligible\t-\t-")
if sys.argv[1] == "wait-local":
    calls = root / "wait-local-calls"
    count = len(calls.read_text().splitlines()) if calls.exists() else 0
    with calls.open("a") as stream:
        _ = stream.write("wait-local\n")
    if count >= 1:
        (root / "hold-waiting").touch()
        wait_until((root / "hold-release").exists, root)
        raise SystemExit(int(os.environ["TEST_HOLD_STATUS"]))
""")
        return {**self.environment, "UNIT_RUN_DIR": str(board),
                "UNIT_TEAM_ROLE": "impl", "BUILD_TARGETS_SCRIPT": str(stub),
                "TEST_HOLD_ROOT": str(self.root), "TEST_HOLD_STATUS": str(wait_status),
                "TEST_REPO": str(VERIFY.parents[2])}

    def start_hold_waiter(self, environment: dict[str, str], output: Path) -> subprocess.Popen[bytes]:
        with output.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                       cwd=self.root, env=environment, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        self.wait_for_running(self.root / "hold-waiting", process)
        return process

    def start_check(self, environment: dict[str, str]) -> subprocess.Popen[str]:
        process = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.started_groups.add(process.pid)
        return process

    def wait_for_waiter(self, board: Path, process: subprocess.Popen[str]) -> None:
        """Waits until the call's own file is in the board's token waiter list."""
        waiters = board / "token_waiters"
        waiters.mkdir(parents=True, exist_ok=True)

        def listed() -> bool:
            return any(path.name.endswith(f".{process.pid}") for path in waiters.iterdir())
        wait_until(lambda: listed() or process.poll() is not None, waiters, processes=(process,))
        self.assertTrue(listed(), "verify never waited for the token")

    def assert_interrupted_holder_stops_step_before_release(self, interruption: signal.Signals) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_CHECK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 preexec_fn=restore_sigint)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists())
            lock = board / "locks" / "cargo.d"
            step_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)

            os.killpg(first.pid, interruption)
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)
            self.assertNotEqual(first.returncode, 0)
            self.assertFalse(lock.exists(), "interrupted verify kept its token")
            self.assertEqual(self.live_group_processes(step_group), [],
                             "token became free while its step still ran")
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        finally:
            _ = (self.root / "release").touch()
            self.end_group(first.pid)
            _ = first.wait(timeout=TEST_HANG_GUARD_SECS)

    def detach_environment(self, status: int) -> dict[str, str]:
        """A Codex worker's environment whose mesh_runs.py stub records its argv and exits `status`."""
        stub = self.root / "mesh_runs_stub.py"
        _ = stub.write_text("import json, os, sys\n"
                            + "with open(os.environ['TEST_MESH_RUNS_CALLS'], 'a', encoding='utf-8') as calls:\n"
                            + "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                            + "raise SystemExit(int(os.environ['TEST_MESH_RUNS_STATUS']))\n")
        return {**self.environment, "CODEX_THREAD_ID": "thread-worker", "VERIFY_MESH_RUNS_SCRIPT": str(stub),
                "TEST_MESH_RUNS_CALLS": str(self.root / "mesh-runs-calls"), "TEST_MESH_RUNS_STATUS": str(status)}

    def mesh_runs_calls(self) -> list[list[str]]:
        path = self.root / "mesh-runs-calls"
        if not path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
