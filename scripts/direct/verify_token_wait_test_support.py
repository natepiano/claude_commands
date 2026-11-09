"""Independent tools, state and process cleanup for verify token tests."""
from __future__ import annotations

from scripts.build_queue import admission_records

from scripts.build_queue.admission_test_support import caller_admission

from scripts.lib.py_floor_test_support import seed_interpreter_cache

from scripts.lint.memory_test_support import config_environment, write_pressure_reading

from collections.abc import Callable

import json
import os
import signal
import shlex
import shutil
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar, cast, override
from scripts.lib.process_group import send_process_group
from scripts.direct.wait_for_test_state import wait_until
from scripts.direct.recorder_fixture import invoke_recorder, record_wait_workers
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.direct.verify_mac_offload_test_support import RUSTC_STUB, stub_build_tool_path
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.shell_test_support import shell_functions


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
        _ = caller_admission(self, self.root, self.environment)

    def build_fixture(self) -> None:
        _ = (self.root / "build_targets.conf").write_text(
            "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
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
    if [ -n "${TEST_TOKEN_OBSERVER:-}" ]; then
        python3 "$TEST_TOKEN_OBSERVER" run || exit 96
    fi
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
        for name in ("tmp", "runtime", "state", "cache", "session", "target", "cgroup", "unit"):
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
        write_pressure_reading(pressure)
        # Interpreter selection is independent of token ownership. Seed the
        # shim's normal cache once rather than rediscovering Python per test.
        seed_interpreter_cache(self.root / "cache")
        memory_event = self.root / "memory-event.py"
        _ = memory_event.write_text(
            "import os\nfrom pathlib import Path\n"
            + f"with open({str(VERIFY.parents[2] / 'scripts/lib/package_root.py')!r}) as package_root: exec(package_root.read(), {{'__file__': package_root.name}})\n"
            + "from scripts.direct.wait_for_test_state import wait_until\n"
            + "path = Path(os.environ['BUILDLOG_MEMINFO'])\n"
            + "wait_until(lambda: len(values := path.read_text().split()) > 1 and values[1] != '0', path.parent, "
            + f"timeout={TEST_HANG_GUARD_SECS})\n")
        # Memory changes are events. Token retry tests may replace this stub
        # with their own acquisition marker or clock advancement.
        self.write_wait_sleep(
            'if [ "$1" = "${BUILDLOG_MEM_POLL_S:-5}" ]; then\n'
            + '    read -r label available unit < "$BUILDLOG_MEMINFO"\n'
            + '    if [ "$available" = 0 ]; then exec python3 "$TEST_MEMORY_EVENT"; fi\n'
            + 'fi\nexit 0\n')
        self.environment = {
            "UNIT_DETACHED_RUN": "1",
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
            "BUILDLOG_MEMORY_PRESSURE": str(pressure),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "hold-release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "ROSTER_RUN_ROOT": str(self.root / "unit"),
            "UNIT_RUN_ROOT": str(self.root / "unit"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
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
            "LINT_CONFIG_FILE": str(reader), **config_environment(self.root),
            "UNIT_SESSION_DIR": str(self.root / "session"),
            "TEST_RUNNING": str(self.root / "running"),
            "TEST_RELEASE": str(self.root / "release"),
            "TEST_WAIT_HELPER": str(VERIFY.parent / "wait_for_test_state.py"),
            "TEST_CARGO_PID": str(self.root / "cargo-pid"),
            "TEST_MEMORY_EVENT": str(memory_event),
        }

    def write_wait_sleep(self, body: str) -> None:
        """Fake queue delays. Keep renewal and other delays on the real clock."""
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token fixture requires sleep on PATH")
        path = self.root / "stubs" / "sleep"
        _ = path.write_text(
            '#!/bin/sh\n'
            + 'case "$1" in "${BUILDLOG_MEM_POLL_S:-5}"|3) ;;\n'
            + f'    *) exec {shlex.quote(real_sleep)} "$@" ;;\nesac\n'
            + body)
        path.chmod(0o755)

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
            send_process_group(group, signal.SIGKILL)
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
        record_wait_workers(self.root, session, self.environment,
                            {f"fixture-{slot}": f"thread-{slot}" for slot in ("impl", "first", "second")})
        self.environment.update({"UNIT_HISTORY_DIR": str(self.root / "history"),
                                 "UNIT_PHASE_ID": "6", "UNIT_SYNC_WRITES": "0",
                                 "UNIT_PASS_OWNER": "launcher", "CODEX_THREAD_ID": fixture_runtime_id("thread-impl"),
                                 "CLAUDE_PROBE_SESSION": ""})
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.root),
             "--main-family", "codex", "--main-model", "fixture", "--main-effort", "high",
             "--main-session-id", fixture_runtime_id("fixture-director")],
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

    def defer_memory_waiter_for_peer(self) -> Callable[[], None]:
        """Keep the first waiter held while a peer uses the same host reading."""
        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            waiting, = admission.server.waiters.values()
            need = waiting.row["need"]
            waiting.row["need"] = 60 * admission_records.GIB
            admission.free(48 * admission_records.GIB)

        def resume() -> None:
            with admission.loop_turn():
                waiting.row["need"] = need
                admission.free(48 * admission_records.GIB)

        self.addCleanup(resume)
        return resume

    def start_memory_waiter(self, environment: dict[str, str], errors: Path) -> subprocess.Popen[bytes]:
        """Start a verify that takes the token and then waits on the held meminfo.

        verify.sh tees its step output to stdout, so both streams go to `errors`.
        """
        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            if not admission.server.reservations and not admission.server.slots:
                _ = admission.occupy_host()
        with errors.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                       cwd=self.root, env=environment, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        self.wait_for_output(errors, "waiting for memory", process)
        self.assertIsNone(process.poll(), errors.read_text(errors="replace"))
        return process

    def wait_for_output(self, path: Path, text: str, process: subprocess.Popen[bytes]) -> None:
        wait_until(lambda: text in path.read_text(errors="replace") or process.poll() is not None,
                   path.parent, processes=(process,), watch_writes=True)
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
with open(Path(os.environ["TEST_REPO"]) / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.direct.wait_for_test_state import wait_until
root = Path(os.environ["TEST_HOLD_ROOT"])
if sys.argv[1] == "eligible":
    print("natedev_only test target")
    print("natedev\teligible\t-\t-\t-")
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

    def start_owned_step(self, environment: dict[str, str]) -> subprocess.Popen[str]:
        """Run the real command guard and renewer with a real board lease."""
        source = VERIFY.read_text()
        start = source.index("STEP_OWNER_PY='\n")
        end = source.index("\nrelease_token() {", start)
        script = self.root / "owned-step.sh"
        _ = script.write_text('set -eo pipefail\nsource "$1"\n'
            + source[start:end] + shell_functions(VERIFY, "release_token") + '''
BOARD_HELPER=$TEST_BOARD_HELPER BOARD_DIR=$UNIT_RUN_DIR BOARD_SLOT=$UNIT_TEAM_ROLE
STEP_RECORD_SCRIPT=$TEST_STEP_RECORD_SCRIPT
PY=$TEST_REAL_PYTHON
source "${STEP_RECORD_SCRIPT%/*}/../lib/conf.sh"
TOKEN_HOLD_S=$(conf_value direct VERIFY_TOKEN_HOLD_SECONDS)
TOKEN_RENEW_S=$(conf_value direct VERIFY_TOKEN_RENEW_SECONDS)
TOKEN_HELD=0 TOKEN_WAIT_FILE=""
HOLD_WAIT_DIR=$TEST_HOLD_DIRECTORY
mkdir -p "$HOLD_WAIT_DIR"
trap 'status=$?; release_token || true; exit "$status"' EXIT
bash "$BOARD_HELPER" acquire "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$ --hold "$TOKEN_HOLD_S"
TOKEN_HELD=1
status=0
buildlog_exec cargo check > "$HOLD_WAIT_DIR/command-output" 2>&1 || status=$?
cat -- "$HOLD_WAIT_DIR/command-output"
exit "$status"
''')
        process = subprocess.Popen(["bash", str(script), str(VERIFY.parent.parent / "lint/invoke.sh")],
            cwd=self.root, env={**environment, "TEST_BOARD_HELPER": str(BOARD),
                "TEST_STEP_RECORD_SCRIPT": str(VERIFY.with_name("step_record.py")),
                "TEST_REAL_PYTHON": sys.executable, "TEST_HOLD_DIRECTORY": str(self.root / "owned-step-hold")},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(process.pid)
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

            send_process_group(first.pid, interruption)
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
        """A Codex worker's environment whose detached_runs.py stub records its argv and exits `status`."""
        stub = self.root / "detached_runs_stub.py"
        _ = stub.write_text("import json, os, sys\n"
                            + "with open(os.environ['TEST_DETACHED_RUNS_CALLS'], 'a', encoding='utf-8') as calls:\n"
                            + "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                            + "raise SystemExit(int(os.environ['TEST_DETACHED_RUNS_STATUS']))\n")
        return {**self.environment, "UNIT_DETACHED_RUN": "", "CODEX_THREAD_ID": "thread-worker", "VERIFY_DETACHED_RUNS_SCRIPT": str(stub),
                "TEST_DETACHED_RUNS_CALLS": str(self.root / "detached-runs-calls"), "TEST_DETACHED_RUNS_STATUS": str(status)}

    def detached_runs_calls(self) -> list[list[str]]:
        path = self.root / "detached-runs-calls"
        if not path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
