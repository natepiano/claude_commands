#!/usr/bin/env python3
"""Exercise verify.sh Mac offload routing with disposable command stand-ins."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import contextlib
from contextlib import contextmanager
from collections.abc import Callable, Generator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest import mock

from scripts.build_hold import build_hold


VERIFY = Path(__file__).with_name("verify.sh")
# Paths and process exits advance tests; this is only a hung-process guard.
HANG_GUARD_S = 300


def stub_build_tool_path(directory: Path) -> str:
    """Use private build stubs and wrappers for shell tools."""
    for name in (
        "bash", "sh", "python3", "git", "awk", "sed", "grep", "tee", "date",
        "sleep", "mktemp", "dirname", "basename", "cat", "head", "uname", "tr",
        "cut", "find", "xargs", "ps", "kill", "readlink", "stat", "mkdir", "rm",
        "mv", "cp", "sort", "wc", "touch", "env", "chmod", "id", "hostname",
        "flock", "timeout", "seq", "tail", "realpath", "perl", "nproc", "getconf",
        "lscpu", "du", "df", "journalctl", "which", "setsid", "ln", "printf",
        "cmp", "rmdir", "true", "false", "install", "readelf", "file",
    ):
        target = directory / name
        source = shutil.which(name)
        if source and not target.exists():
            _ = target.write_text(f'#!/bin/sh\nexec {shlex.quote(source)} "$@"\n')
            target.chmod(0o755)
    for name in (
        "cargo", "rustc", "rustdoc", "rustup", "nextest", "cargo-nextest",
        "clippy-driver", "cargo-clippy", "rustfmt", "cargo-fmt", "cargo-mend",
        "sccache", "cc", "gcc", "clang", "c++", "g++", "clang++", "make",
        "cmake", "ninja", "ld", "lld", "ar",
    ):
        target = directory / name
        if not target.exists():
            _ = target.write_text('#!/bin/sh\necho "Unexpected build tool: $0" >&2\nexit 99\n')
            target.chmod(0o755)
    return str(directory)


def restore_sigint() -> None:
    # Background suite processes may inherit SIG_IGN; Bash cannot trap that.
    _ = signal.signal(signal.SIGINT, signal.SIG_DFL)


def processes_carrying(marker: str) -> set[int]:
    """Live processes, other than this one, whose environment holds `marker`; a zombie's reads empty."""
    needle = marker.encode()
    found: set[int] = set()
    for entry in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if needle in (entry / "environ").read_bytes().split(b"\0"):
                found.add(int(entry.name))
        except OSError:
            continue
    return found


def reap(marker: str) -> set[int]:
    """Kill every process carrying `marker` and wait until none is left.

    verify.sh runs each step in its own process group and its step owner in
    its own session, so a kill of verify's group leaves the step's tree
    running under init. A stopped process cannot fork or exit, so stopping
    each one found until a scan finds no new one catches the whole tree.
    """
    stopped: set[int] = set()
    while fresh := processes_carrying(marker) - stopped:
        for pid in fresh:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGSTOP)
        stopped |= fresh
    for pid in stopped:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + HANG_GUARD_S
    while processes_carrying(marker) & stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    return stopped


class CargoEvent(TypedDict):
    args: list[str]
    token_held: bool


class RunnerEvent(TypedDict):
    args: list[str]
    token_held: bool
    at: float


class RoutingEvent(TypedDict):
    command: str
    status: int
    at: float
    pid: int


class RoutingRecord(TypedDict):
    target: str
    hold_wait_s: float
    mem_wait_s: str
    copy: str
    copy_s: str
    copy_count: str
    copy_natedev_s: str


class CallRecord(TypedDict):
    kind: str
    command: str
    outcome: str
    status: int | None
    token_wait_s: int
    mac: str
    mac_reason: str
    mac_holder: str
    local_reason: str
    mac_s: float | None


class BusyClaimRecord(TypedDict):
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str


class CargoTargetMetadata(TypedDict):
    name: str
    kind: list[str]
    test: bool
    src_path: str


class CargoPackageMetadata(TypedDict):
    name: str
    id: str
    features: dict[str, list[str]]
    manifest_path: str
    dependencies: list[dict[str, str]]
    targets: list[CargoTargetMetadata]


class CargoMetadata(TypedDict):
    workspace_root: str
    workspace_members: list[str]
    packages: list[CargoPackageMetadata]


CARGO_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
event = {
    "args": arguments,
    "token_held": Path(os.environ["TEST_TOKEN_RECORD"]).exists(),
}
with Path(os.environ["TEST_CARGO_EVENTS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")

if arguments[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
    raise SystemExit(0)
if arguments[:2] == ["nextest", "--version"]:
    raise SystemExit(0)
if arguments[:2] == ["nextest", "run"] and os.environ.get("TEST_CARGO_HOLD") == "1":
    # A natedev step still compiling: a cargo line, then a wait for
    # cargo-release (tests start) or SIGTERM (a Mac copy won).
    import signal
    import time

    root = Path(os.environ["TEST_RUNNER_STATE"])

    def stop(signal_number: int, _frame: object) -> None:
        (root / "cargo-stopped").write_text(str(signal_number), encoding="utf-8")
        raise SystemExit(128 + signal_number)

    signal.signal(signal.SIGTERM, stop)
    print("   Compiling sample v0.1.0", flush=True)
    (root / "cargo-compiling").write_text(f"{os.getpid()}\n", encoding="utf-8")
    while not (root / "cargo-release").exists():
        time.sleep(0.01)
    if os.environ.get("TEST_CARGO_STARTING", "1") == "1":
        print("     Starting 1 test", flush=True)
if arguments[:2] == ["nextest", "run"]:
    raise SystemExit(int(os.environ.get("TEST_LOCAL_STATUS", "0")))
raise SystemExit(0)
'''


GIT_STUB = r'''#!/bin/bash
has() { local word; for word in "$@"; do [[ "$word" == "$wanted" ]] && return 0; done; return 1; }
wanted=rev-parse
if has "$@"; then
    wanted='HEAD^{tree}'
    if has "$@"; then printf 'fixed-tree\n'; exit 0; fi
    wanted=--show-toplevel
    if has "$@"; then
        wanted=--git-common-dir
        if has "$@"; then
            printf '%s\n' "$PACKAGE_ROOT/.git" "$PACKAGE_ROOT"
            wanted=--abbrev-ref
            if has "$@"; then printf 'main\n'; fi
        else
            printf '%s\n' "$PACKAGE_ROOT"
        fi
        exit 0
    fi
    wanted=--short
    if has "$@"; then printf 'abc1234\n'; exit 0; fi
fi
for wanted in status diff ls-files ls-tree; do if has "$@"; then exit 0; fi; done
exit 1
'''


RUSTC_STUB = r'''#!/bin/bash
if [[ $# == 1 && $1 == -vV ]]; then
    printf 'rustc 1.90.0\nhost: x86_64-unknown-linux-gnu\n'
elif [[ $# == 1 && $1 == -V ]]; then
    printf 'rustc 1.90.0\n'
else
    exit 1
fi
'''


RUNNER_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time

arguments = sys.argv[1:]
root = Path(os.environ["TEST_RUNNER_STATE"])
result = Path(arguments[arguments.index("--result") + 1])

if "--copy" in arguments:
    # A Mac copy started by copy_watch.py. With TEST_COPY_CLAIM (mac_test.py)
    # it holds a real copy claim; with TEST_COPY_WAIT=1 it waits for
    # copy-release or SIGTERM; then it writes TEST_COPY_MAC and exits
    # TEST_COPY_STATUS. SIGTERM releases the claim and exits 143.
    import subprocess

    mac_test = os.environ.get("TEST_COPY_CLAIM", "")
    claimed = False

    def release_copy_claim() -> None:
        if claimed:
            subprocess.run([sys.executable, mac_test, "release", "--pid", str(os.getpid())],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_copy(signal_number: int, _frame: object) -> None:
        release_copy_claim()
        # Renamed into place, so the test never reads it half written.
        (root / "copy-signal.tmp").write_text(str(signal_number), encoding="utf-8")
        os.replace(root / "copy-signal.tmp", root / "copy-signal")
        raise SystemExit(128 + signal_number)

    signal.signal(signal.SIGTERM, stop_copy)
    if mac_test:
        claim = subprocess.run(
            [sys.executable, mac_test, "claim", "--copy", "--pid", str(os.getpid()), "--what", "nextest sample",
             "--worktree", arguments[arguments.index("--repo-root") + 1]],
            check=False, capture_output=True, text=True)
        if claim.returncode != 0:
            print("copy claim failed: " + claim.stdout + claim.stderr, file=sys.stderr, flush=True)
            raise SystemExit(99)
        claimed = True
    with (root / "copy-events.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(arguments) + "\n")
    # Renamed into place, so the test never reads it before the pid is written.
    (root / "copy-started.tmp").write_text(str(os.getpid()), encoding="utf-8")
    os.replace(root / "copy-started.tmp", root / "copy-started")
    if os.environ.get("TEST_COPY_WAIT") == "1":
        while not (root / "copy-release").exists():
            time.sleep(0.01)
    result.write_text("mac=" + os.environ.get("TEST_COPY_MAC", "failed") + "\nreason=\nseconds="
                      + os.environ.get("TEST_COPY_SECONDS", "2.5") + "\n", encoding="utf-8")
    release_copy_claim()
    raise SystemExit(int(os.environ.get("TEST_COPY_STATUS", "1")))

event = {
    "args": arguments,
    "token_held": Path(os.environ["TEST_TOKEN_RECORD"]).exists(),
    "at": time.monotonic(),
}
with (root / "runner-events.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")
(root / "runner-started").write_text("started\n", encoding="utf-8")

attempt = len((root / "runner-events.jsonl").read_text().splitlines())
last_decline = arguments[arguments.index("--last-decline") + 1] if "--last-decline" in arguments else ""

def decline(reason: str, seconds: str) -> None:
    holder = "holder=" + os.environ.get("TEST_RUNNER_HOLDER", "other tests") + "\n" if reason == "busy" else ""
    result.write_text("mac=declined\nreason=" + reason + "\nseconds=" + seconds + "\n" + holder)
    if reason not in ("busy", "blocked") or reason != last_decline:
        print("claim diagnostic: " + reason, file=sys.stderr, flush=True)
        print("mac_test: not running on the Mac (" + reason + ")", flush=True)

if os.environ.get("TEST_RUNNER_BUSY_UNTIL") and not Path(os.environ["TEST_RUNNER_BUSY_UNTIL"]).exists():
    reason_file = root / "busy-reason"
    reason = reason_file.read_text().strip() if reason_file.exists() else os.environ.get("TEST_RUNNER_BUSY_REASON", "busy")
    decline(reason, os.environ.get("TEST_DECLINED_RETRY_SECONDS", "0") if attempt > 1 else "0")
    raise SystemExit(75)
if attempt > 1 and os.environ.get("TEST_RUNNER_PAUSE_RETRY") == "1":
    (root / "retry-running").write_text("running\n")
    while not (root / "continue-retry").exists():
        time.sleep(0.01)

if os.environ.get("TEST_RUNNER_HOLD") == "1":
    def stop(signum: int, _frame: object) -> None:
        (root / "runner-signal").write_text(str(signum), encoding="utf-8")
        raise SystemExit(128 + signum)

    for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signal_number, stop)
    # Publish readiness only after every forwarded signal has a handler.
    (root / "runner-signals-ready").write_text("ready\n", encoding="utf-8")
    while True:
        signal.pause()

mode = os.environ.get("TEST_RUNNER_MAC", "declined")
reason = os.environ.get("TEST_RUNNER_REASON", "")
runner_status = int(os.environ.get("TEST_RUNNER_STATUS", "75"))
answer = root / "runner-answer"
if answer.exists():
    mode, reason, status_text = answer.read_text().splitlines()
    runner_status = int(status_text)
if mode in ("passed", "failed", "lost"):
    print("mac_test: running on the Mac (macOS, host Mac); tree copied in 0.0 s", flush=True)
if mode == "none":
    result.unlink(missing_ok=True)
elif mode == "empty":
    result.write_text("", encoding="utf-8")
elif mode == "directory":
    result.unlink(missing_ok=True)
    result.mkdir()
else:
    result.write_text(
        "mac=" + mode + "\n"
        + "reason=" + reason + "\n"
        + "seconds=" + os.environ.get("TEST_RUNNER_SECONDS", "3.25") + "\n",
        encoding="utf-8",
    )
if mode == "declined":
    decline(reason, os.environ.get("TEST_RUNNER_SECONDS", "3.25"))
output = os.environ.get("TEST_RUNNER_OUTPUT", "")
if output and not (mode == "declined" and last_decline in ("busy", "blocked") and last_decline == os.environ.get("TEST_RUNNER_REASON", "")):
    print(output, flush=True)
raise SystemExit(runner_status)
'''


TARGETS_WRAPPER = r'''
import json
import os
from pathlib import Path
import runpy
import sys
import time

command = sys.argv[1]
root = Path(os.environ["TEST_RUNNER_STATE"])
if command == "wait-local":
    (root / "local-wait-args").write_text(json.dumps(sys.argv[2:]))
if command == "wait-local" and os.environ.get("TEST_LOCAL_WAIT_SECONDS") and not (root / "local-wait-added").exists():
    with Path(os.environ["BUILDLOG_HOLD_WAIT_FILE"]).open("a") as output:
        output.write(os.environ["TEST_LOCAL_WAIT_SECONDS"] + "\n")
    (root / "local-wait-added").write_text("added\n")
sys.path.insert(0, str(Path(os.environ["TEST_REAL_TARGETS"]).parent))
status = 0
try:
    runpy.run_path(os.environ["TEST_REAL_TARGETS"], run_name="__main__")
except SystemExit as result:
    status = result.code or 0
event = {"command": command, "status": status, "at": time.monotonic(),
         "pid": int(sys.argv[sys.argv.index("--pid") + 1]) if "--pid" in sys.argv else 0}
with (root / "routing-events.jsonl").open("a") as output:
    output.write(json.dumps(event) + "\n")
if command == "register-local" and status == 10:
    (root / "registered").write_text("registered\n")
raise SystemExit(status)
'''


SLEEP_STUB = r'''
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["TEST_RUNNER_STATE"])
with (root / "sleep-events").open("a") as output:
    output.write(sys.argv[1] + "\n")
if os.environ.get("TEST_STEP_ROUTING_SLEEP") == "1":
    step = len((root / "sleep-events").read_text().splitlines())
    (root / "routing-sleep").write_text("sleeping\n")
    (root / ("routing-sleep-" + str(step))).write_text("sleeping\n")
    while not (root / ("continue-sleep-" + str(step))).exists():
        time.sleep(0.01)
elif os.environ.get("TEST_PAUSE_ROUTING_SLEEP") == "1":
    (root / "routing-sleep").write_text("sleeping\n")
    while not (root / "continue-sleep").exists():
        time.sleep(0.01)
else:
    time.sleep(min(float(sys.argv[1]), 0.05))
'''


# mac_test.py free-now as verify.sh asks it: busy while the runner stub's
# TEST_RUNNER_BUSY_UNTIL file is missing, so both answer alike. With
# TEST_FREE_NOW_ONCE, it answers free once, when that file exists, and busy
# after; a call after that one free answer writes free-now-after-leave.
# TEST_FREE_NOW_FREE=1 answers free always, so each routing pass tries a claim
# that may still decline. Each call's words after the settings path go to
# free-now-roots.
FREE_NOW_STUB = r'''
import os
from pathlib import Path
import sys

root = Path(os.environ["TEST_RUNNER_STATE"])
with (root / "free-now-roots").open("a") as output:
    output.write(" ".join(sys.argv[4:]) + "\n")
assert sys.argv[1:3] == ["free-now", "--settings"] and Path(sys.argv[3]).is_file(), sys.argv
with (root / "free-now-events").open("a") as output:
    output.write(sys.argv[3] + "\n")
once = os.environ.get("TEST_FREE_NOW_ONCE", "")
busy_until = os.environ.get("TEST_RUNNER_BUSY_UNTIL", "")
if os.environ.get("TEST_FREE_NOW_FREE") == "1":
    free = True
elif once:
    if Path(once).exists():
        Path(once).unlink()
        (root / "free-now-left").write_text("left\n")
        free = True
    else:
        if (root / "free-now-left").exists():
            (root / "free-now-after-leave").write_text("asked\n")
        free = False
else:
    free = not busy_until or Path(busy_until).exists()
print("free" if free else "busy")
raise SystemExit(0 if free else 1)
'''


RECORD_WRAPPER = r'''
import json
import os
from pathlib import Path
import runpy
import sys

if sys.argv[1:2] == ["call"]:
    event = {"target": os.environ.get("BUILDLOG_TARGET", ""),
             "hold_wait_s": float(os.environ.get("BUILDLOG_HOLD_WAIT_S", "0")),
             "mem_wait_s": os.environ.get("BUILDLOG_MEM_WAIT_S", "0"),
             "copy": os.environ.get("BUILDLOG_MAC_COPY", ""),
             "copy_s": os.environ.get("BUILDLOG_MAC_COPY_S", ""),
             "copy_count": os.environ.get("BUILDLOG_MAC_COPY_COUNT", ""),
             "copy_natedev_s": os.environ.get("BUILDLOG_MAC_COPY_NATEDEV_S", "")}
    with (Path(os.environ["TEST_RUNNER_STATE"]) / "routing-records.jsonl").open("a") as output:
        output.write(json.dumps(event) + "\n")
sys.path.insert(0, str(Path(os.environ["TEST_REAL_RECORD"]).parent))
runpy.run_path(os.environ["TEST_REAL_RECORD"], run_name="__main__")
'''


ROUTING_CLOCK_SHIM = r'''
import os
import sys

# Run verify's clock expressions against a clock advanced at routing gates.
# Other Python invocations still use the real interpreter and scripts.
if len(sys.argv) > 2 and sys.argv[1] == "-c" and "TEST_ROUTING_CLOCK" in os.environ:
    if "time.monotonic() <" in sys.argv[2]:
        from pathlib import Path
        with (Path(os.environ["TEST_RUNNER_STATE"]) / "retry-probes").open("a") as output:
            output.write(Path(os.environ["TEST_ROUTING_CLOCK"]).read_text() + "\n")
    code = sys.argv[2].replace("time.monotonic()",
        'float(__import__("pathlib").Path(__import__("os").environ["TEST_ROUTING_CLOCK"]).read_text())')
    sys.argv = ["-c", *sys.argv[3:]]
    exec(compile(code, "<routing-clock>", "exec"))
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
'''


@final
class VerifyMacOffloadTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.bin_directory = Path()
        self.board = Path()
        self.runner = Path()
        self.runner_state = Path()
        self.cargo_events_path = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.addCleanup(self.assert_nothing_outlives)
        self.home = self.root / "home"
        self.home.mkdir()
        claude = self.home / ".claude"
        scripts = claude / "scripts"
        scripts.mkdir(parents=True)
        (claude / "config").symlink_to(VERIFY.parent.parent.parent / "config", target_is_directory=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "buildlog":
                (scripts / source.name).symlink_to(source, target_is_directory=source.is_dir())
        log = scripts / "buildlog"
        log.mkdir()
        self.write_executable(log / "record.py", RECORD_WRAPPER)
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        self.board = self.root / "board"
        self.runner_state = self.root / "runner-state"
        self.runner_state.mkdir()
        self.cargo_events_path = self.root / "cargo-events.jsonl"
        self.runner = self.root / "runner.py"
        self.write_executable(self.bin_directory / "cargo", CARGO_STUB)
        self.write_executable(self.bin_directory / "git", GIT_STUB)
        self.write_executable(self.bin_directory / "rustc", RUSTC_STUB)
        self.write_executable(self.bin_directory / "taplo", "#!/bin/sh\nexit 0\n")
        self.write_executable(self.runner, RUNNER_STUB)
        self.write_executable(self.root / "free-now.py", FREE_NOW_STUB)
        blocker = "#!/bin/sh\nexit 99\n"
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.write_executable(self.bin_directory / name, blocker)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text(
            "lint_config_enabled() { return 1; }\n"
            + "lint_config_skip_notice() { :; }\n",
            encoding="utf-8",
        )
        for name in (
            "tmp",
            "runtime",
            "state",
            "cache",
            "target",
            "cgroup",
            "cargo-home",
            ".git",
        ):
            (self.root / name).mkdir()
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n',
            encoding="utf-8",
        )
        source = self.root / "src" / "lib.rs"
        source.parent.mkdir()
        _ = source.write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        settings = self.root / "mac settings.conf"
        _ = settings.write_text(f"offload=on\nlinux_only.{self.root.name}=\n")
        config = self.root / "targets.conf"
        _ = config.write_text("targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\n"
                              + f"settings.mac={settings.name}\nkind.natedev=local\nplatform.natedev=linux\n")
        metadata: CargoMetadata = {
            "workspace_root": str(self.root),
            "workspace_members": ["sample-id"],
            "packages": [
                {
                    "name": "sample",
                    "id": "sample-id",
                    "features": {"default": [], "extra": []},
                    "manifest_path": str(self.root / "Cargo.toml"),
                    "dependencies": [],
                    "targets": [
                        {
                            "name": "sample",
                            "kind": ["lib"],
                            "test": True,
                            "src_path": str(source),
                        }
                    ],
                }
            ],
        }
        self.environment = {
            **{
                name: value
                for name, value in os.environ.items()
                if name
                not in {
                    "UNIT_RUN_DIR",
                    "UNIT_SESSION_DIR",
                    "UNIT_TEAM_ROLE",
                    "CODEX_THREAD_ID",
                    "CODEX_SESSION_ID",
                    "UNIT_DETACHED_RUN",
                    "BUILDLOG_MAC",
                    "BUILDLOG_MAC_REASON",
                    "BUILDLOG_LOCAL_REASON",
                    "BUILDLOG_MAC_S",
                    "BASH_ENV",
                    "ENV",
                }
                and not name.startswith("BASH_FUNC_")
            },
            "HOME": str(self.home),
            "PATH": stub_build_tool_path(self.bin_directory),
            "RUSTUP_HOME": str(self.root / "rustup"),
            "PACKAGE_ROOT": str(self.root),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILD_TARGETS_SCRIPT": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),
            "BUILD_TARGETS_CONFIG": str(config),
            "MAC_TEST_CONFIG": str(settings),
            "CLAUDE_CODE_SESSION_ID": "", "BUILD_TARGETS_NOW": "",
            "BUILD_TARGETS_POLL_S": "0.05",
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader),
            "UNIT_RUN_DIR": str(self.board),
            "UNIT_TEAM_ROLE": "test",
            "VERIFY_MAC_RUNNER": str(self.runner),
            "VERIFY_MAC_COPY_WATCH": str(VERIFY.parent.parent / "mac_test" / "copy_watch.py"),
            "VERIFY_MAC_TEST_SCRIPT": str(self.root / "free-now.py"),
            "TEST_RUNNER_STATE": str(self.runner_state),
            "TEST_CARGO_EVENTS": str(self.cargo_events_path),
            "TEST_TOKEN_RECORD": str(self.board / "locks" / "cargo.d" / "owned"),
            "TEST_METADATA": json.dumps(metadata),
            "TEST_RUNNER_SECONDS": "3.25",
            "TEST_REAL_RECORD": str(VERIFY.parent.parent / "buildlog" / "record.py"),
        }

        cache = self.root / "cache/claude/python3-3.10"
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(sys.executable + "\n")
        _ = self.environment.pop("CLAUDE_PYTHON", None)

    def test_build_tools_cannot_use_the_host_path(self) -> None:
        host_tools = self.root / "host-tools"
        host_tools.mkdir()
        reached = self.root / "host-build-tool-ran"
        for name in ("cargo", "rustc", "nextest"):
            self.write_executable(host_tools / name, f'#!/bin/sh\ntouch "{reached}"\nexit 98\n')
        private_tools = self.root / "private-tools"
        private_tools.mkdir()
        with mock.patch.dict(os.environ, {"PATH": f"{host_tools}{os.pathsep}{os.environ['PATH']}"}):
            private_path = stub_build_tool_path(private_tools)
        for name in ("cargo", "rustc", "nextest"):
            with self.subTest(tool=name):
                result = subprocess.run([name, "--version"], env={**self.environment, "PATH": private_path},
                                        capture_output=True, text=True, check=False, timeout=HANG_GUARD_S)
                self.assertEqual(result.returncode, 99, result.stdout + result.stderr)
        self.assertFalse(reached.exists())

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text(source if source.startswith("#!") else f"#!{sys.executable}\n" + source,
                            encoding="utf-8")
        path.chmod(0o755)

    def verify(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(VERIFY), *arguments],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=HANG_GUARD_S,
        )

    def start_verify(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["bash", str(VERIFY), *arguments],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=restore_sigint,
        )

    def cargo_events(self) -> list[CargoEvent]:
        if not self.cargo_events_path.exists():
            return []
        return [
            cast(CargoEvent, json.loads(line))
            for line in self.cargo_events_path.read_text(encoding="utf-8").splitlines()
        ]

    def runner_events(self) -> list[RunnerEvent]:
        path = self.runner_state / "runner-events.jsonl"
        if not path.exists():
            return []
        return [
            cast(RunnerEvent, json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def local_runs(self) -> list[CargoEvent]:
        return [
            event
            for event in self.cargo_events()
            if event["args"][:2] == ["nextest", "run"]
        ]

    def call_records(self) -> list[CallRecord]:
        return [
            cast(CallRecord, json.loads(line))
            for path in (self.root / "buildlog").glob("*/*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if cast(dict[str, object], json.loads(line)).get("kind") == "call"
        ]

    def last_call(self) -> CallRecord:
        records = self.call_records()
        self.assertTrue(records)
        return records[-1]

    def saved_results(self) -> list[Path]:
        cache = self.board / "verify_cache"
        return list(cache.glob("*.pass")) + list(cache.glob("*.fail"))

    def clear_observations(self) -> None:
        self.cargo_events_path.unlink(missing_ok=True)
        for name in ("runner-events.jsonl", "runner-started", "runner-signal", "runner-signals-ready"):
            (self.runner_state / name).unlink(missing_ok=True)

    def wait_for_path(self, path: Path, processes: Sequence[subprocess.Popen[str]] = ()) -> None:
        deadline = time.monotonic() + HANG_GUARD_S
        while time.monotonic() < deadline:
            if path.exists():
                return
            for process in processes:
                if process.poll() is not None:
                    output, errors = process.communicate(timeout=HANG_GUARD_S)
                    self.fail(f"verify exited {process.returncode} before {path}: {output + errors}")
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def routing_fixture(self) -> Path:
        wrapper = self.root / "targets-wrapper.py"
        self.write_executable(wrapper, TARGETS_WRAPPER)
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        routing_wait = self.root / "routing-wait.py"
        self.write_executable(routing_wait, SLEEP_STUB)
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(wrapper),
                                 "VERIFY_LOCAL_RELEASE_WAIT_SCRIPT": str(routing_wait),
                                 "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets" / "build_targets.py")})
        with self.hold_environment():
            holders = Path(self.environment["BUILD_HOLD_DIR"])
            _ = build_hold.start_hold(holders, build_hold.HolderIdentity("hana", "hana"), "render checks", build_hold.NoReleaseEta(), datetime.now().astimezone())
            cycle = build_hold.read_cycle()
            assert cycle is not None
            return self.root / "release" / cycle["id"] / "cycle.json"

    def test_hold_fixture_reaps_its_recorders_without_waiting_for_an_existing_recorder(self) -> None:
        # The real hold dispatcher starts this child and its reaper. Only a
        # join at the fixture boundary releases the child to write completion.
        tree = self.root / "record-tree/scripts"
        (tree / "build_hold").mkdir(parents=True)
        (tree / "buildlog").mkdir()
        release, completed = self.root / "recorder-release", self.root / "recorder-completed"
        self.write_executable(tree / "buildlog/record.py", f'''
from pathlib import Path
from scripts.direct.wait_for_test_state import wait_for_file
wait_for_file(Path({str(release)!r}))
Path({str(completed)!r}).touch()
'''.replace("from scripts.direct.wait_for_test_state import wait_for_file",
            "import sys\nsys.path.insert(0, " + repr(str(VERIFY.resolve().parents[2])) + ")\n"
            + "from scripts.direct.wait_for_test_state import wait_for_file"))
        outside_release = threading.Event()
        outside = threading.Thread(target=outside_release.wait, name="hold-event-recorder", daemon=True)
        outside.start()
        original_join = threading.Thread.join
        owned: list[threading.Thread] = []

        def release_on_join(thread: threading.Thread, timeout: float | None = None) -> None:
            if thread.name == "hold-event-recorder":
                self.assertIsNot(thread, outside, "fixture tried to wait for a recorder it did not start")
                owned.append(thread)
                release.touch()
            original_join(thread, timeout)

        existing = set(threading.enumerate())
        try:
            with mock.patch.object(build_hold, "__file__", str(tree / "build_hold/build_hold.py")), \
                    mock.patch.object(threading.Thread, "join", release_on_join):
                with self.hold_environment():
                    _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]),
                        build_hold.HolderIdentity("hana", "hana"), "fixture recording", build_hold.NoReleaseEta(),
                        datetime.now().astimezone())
                self.assertTrue(completed.exists(), "fixture returned while its recorder still owned its files")
            self.assertEqual(len(owned), 1)
            self.assertFalse(owned[0].is_alive())
            self.assertTrue(outside.is_alive())
        finally:
            release.touch()
            outside_release.set()
            for thread in threading.enumerate():
                if thread is outside or (thread not in existing and thread.name == "hold-event-recorder"):
                    original_join(thread, HANG_GUARD_S)
                    self.assertFalse(thread.is_alive())

    def control_routing_clock(self) -> Path:
        clock = self.runner_state / "routing-clock"
        _ = clock.write_text("100")
        library = self.home / ".claude" / "scripts" / "lib"
        library.unlink()
        library.mkdir()
        self.write_executable(library / "py", ROUTING_CLOCK_SHIM)
        self.environment["TEST_ROUTING_CLOCK"] = str(clock)
        return clock

    def test_hold_fixture_timeout_reaps_all_children_and_owned_threads_before_restoring_home(self) -> None:
        tree = self.root / "blocked-record-tree/scripts"
        (tree / "build_hold").mkdir(parents=True)
        (tree / "buildlog").mkdir()
        ready = self.root / "blocked-recorders"
        ready.mkdir()
        self.write_executable(tree / "buildlog/record.py", f'''
import os, signal, sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}, sys.argv[4]).write_text(str(os.getpid()))
signal.pause()
''')
        children: list[subprocess.Popen[bytes]] = []
        threads: list[threading.Thread] = []
        launch = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)
        start = threading.Thread.start
        caller_home = os.environ.get("HOME")

        def capture_child(*arguments: object, **options: object) -> subprocess.Popen[bytes]:
            child = launch(*arguments, **options)
            children.append(child)
            return child

        def capture_thread(thread: threading.Thread) -> None:
            if thread.name == "hold-event-recorder":
                threads.append(thread)
            start(thread)

        try:
            with mock.patch.object(build_hold, "__file__", str(tree / "build_hold/build_hold.py")), \
                    mock.patch.object(subprocess, "Popen", side_effect=capture_child), \
                    mock.patch.object(threading.Thread, "start", capture_thread):
                with self.assertRaisesRegex(AssertionError, "fixture hold recorder never exited"):
                    with self.hold_environment(timeout=0):
                        for key in ("first", "second"):
                            _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]),
                                build_hold.HolderIdentity(key, key), "blocked recording", build_hold.NoReleaseEta(),
                                datetime.now().astimezone())
                            self.wait_for_path(ready / key)
                self.assertEqual(len(children), 2)
                self.assertEqual(len(threads), 2)
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(os.environ.get("HOME"), caller_home)
        finally:
            # Also contain a deliberately broken implementation during regression checks.
            for child in children:
                if child.poll() is None:
                    child.kill()
                _ = child.wait()
            for thread in threads:
                thread.join(timeout=HANG_GUARD_S)

    @contextmanager
    def hold_environment(self, timeout: float = HANG_GUARD_S) -> Generator[None, None, None]:
        children: list[subprocess.Popen[bytes]] = []
        threads: list[threading.Thread] = []
        launch = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)
        start = threading.Thread.start

        def launch_owned(*arguments: object, **options: object) -> subprocess.Popen[bytes]:
            process = launch(*arguments, **options)
            children.append(process)
            return process

        def start_owned(thread: threading.Thread) -> None:
            if thread.name == "hold-event-recorder":
                threads.append(thread)
            start(thread)

        with mock.patch.dict(os.environ, {name: self.environment[name] for name in (
            "HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
            "BUILD_HOLD_DIR", "BUILD_HOLD_RELEASE_DIR", "UNIT_ACTIVE_DIR",
            "BUILDLOG_DIR", "MAC_TEST_STATE_DIR", "NOTIFIER_SESSIONS_DIR")}), \
             mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
                                          "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": ""}), \
             mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()), \
             mock.patch.object(subprocess, "Popen", side_effect=launch_owned), \
             mock.patch.object(threading.Thread, "start", start_owned):
            try:
                yield
            finally:
                timed_out = False
                for thread in threads:
                    thread.join(timeout=timeout)
                    timed_out |= thread.is_alive()
                if timed_out:
                    # Stop every child before restoring HOME or reporting the
                    # timeout, including children owned by later reapers.
                    for process in children:
                        if process.poll() is None:
                            process.terminate()
                    for process in children:
                        if process.poll() is None:
                            process.kill()
                        _ = process.wait()
                    for thread in threads:
                        thread.join()
                self.assertFalse(timed_out, "fixture hold recorder never exited")

    def release_local_hold(self) -> None:
        with self.hold_environment():
            _ = build_hold.release_cycle(Path(self.environment["BUILD_HOLD_DIR"]), "hana")

    def routing_events(self) -> list[RoutingEvent]:
        path = self.runner_state / "routing-events.jsonl"
        return [cast(RoutingEvent, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def routing_record(self) -> RoutingRecord:
        records = (self.runner_state / "routing-records.jsonl").read_text().splitlines()
        self.assertTrue(records)
        return cast(RoutingRecord, json.loads(records[-1]))

    def child_marker(self) -> str:
        """The environment entry every process this test starts inherits."""
        return f"TEST_RUNNER_STATE={self.runner_state}"

    def stop_verify(self, process: subprocess.Popen[str]) -> None:
        """Kill the call and every process under it, its steps' own groups and sessions too."""
        _ = reap(self.child_marker())
        _ = process.communicate(timeout=HANG_GUARD_S)

    def assert_nothing_outlives(self) -> None:
        # Runs after every other cleanup: a process a call left behind
        # holds no token and no turn, but `build_hold.py quiet` counts it
        # as a cargo run for as long as it lives.
        deadline = time.monotonic() + 2
        while processes_carrying(self.child_marker()) and time.monotonic() < deadline:
            time.sleep(0.01)
        survivors = reap(self.child_marker())
        if survivors:
            self.fail(f"processes outlived the test: {sorted(survivors)}")

    def start_routing(self, reason: str = "busy", *, pause_retry: bool = False,
                      step_sleep: bool = False, once: Path | None = None) -> subprocess.Popen[str]:
        process = self.start_verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            **({"TEST_FREE_NOW_ONCE": str(once)} if once else {}),
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": reason, "TEST_PAUSE_ROUTING_SLEEP": "1",
            "TEST_RUNNER_PAUSE_RETRY": "1" if pause_retry else "0",
            "TEST_STEP_ROUTING_SLEEP": "1" if step_sleep else "0",
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
        })
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "routing-sleep", (process,))
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 1)
        self.assertIsNone(process.poll())
        return process

    def assert_no_pending_calls(self, cycle_path: Path) -> None:
        cycle = cast(build_hold.HoldCycle, json.loads(cycle_path.read_text()))
        self.assertEqual(cycle["entries"], [])

    def test_local_first_target_runs_without_trying_free_mac(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        self.assertIn("verify.sh: running on natedev", result.stdout)
        self.assertEqual(self.routing_record()["target"], "natedev")

    def test_alternate_target_settings_are_passed_to_runner(self) -> None:
        alternate = self.root / "alternate Mac.conf"
        _ = alternate.write_text(f"linux_only.{self.root.name}=\n")
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("mac settings.conf", alternate.name))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache",
                             extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.runner_events()[0]["args"]
        self.assertEqual(arguments[arguments.index("--settings") + 1], str(alternate))
        self.assertIn("mac_test: running on the Mac (macOS, host Mac); tree copied in 0.0 s", result.stdout)
        self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
        self.assertEqual(self.routing_record()["target"], "mac")

    def test_excluded_mac_reason_is_recorded_without_calling_offload(self) -> None:
        for settings, reason in (("", "repo"), (f"linux_only.{self.root.name}=sample\n", "linux_only")):
            with self.subTest(reason=reason):
                self.clear_observations()
                _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(settings)
                result = self.verify("test", "sample", "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(self.last_call()["local_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")

    def test_declined_mac_attempt_announces_only_local_execution(self) -> None:
        for reason in ("busy", "blocked", "disk"):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": "75"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
                self.assertNotIn("mac_test: running on the Mac", result.stdout + result.stderr)
                self.assertIn("verify.sh: running on natedev", result.stdout)
                self.assertEqual(len(self.local_runs()), 1)

    def test_unreadable_local_first_route_refuses_before_either_machine_starts(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        for outcome, reason, status in (("passed", "", "0"), ("declined", "busy", "75")):
            with self.subTest(outcome=outcome):
                self.clear_observations()
                result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
                    "TEST_RUNNER_MAC": outcome, "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": status})
                self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
                self.assertIn("build hold check register-local failed (exit 12); step refused", result.stderr)
                self.assertEqual(self.runner_events(), [])
                self.assertEqual([event for event in self.cargo_events()
                                  if event["args"][:1] != ["metadata"]], [])
                self.assertIn("natedev's hold state cannot be read", result.stderr)

    def test_unreadable_local_first_route_without_eligible_mac_exits_twelve(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(f"linux_only.{self.root.name}=sample\n")
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertIn("natedev's hold state cannot be read", result.stderr)

    def resume_routing_sleep(self, step: int) -> None:
        _ = (self.runner_state / f"continue-sleep-{step}").write_text("continue\n")
        self.wait_for_path(self.runner_state / f"routing-sleep-{step + 1}")

    def test_repeated_routing_wait_shows_each_unchanged_diagnostic_once(self) -> None:
        _ = self.routing_fixture()
        self.environment.update({"BUILD_TARGETS_ESCALATE_S": "0", "TEST_FREE_NOW_FREE": "1"})
        process = self.start_routing(step_sleep=True)
        for step in range(1, 4):
            self.resume_routing_sleep(step)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-4").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(errors.count("verify.sh: natedev is held by hana for render checks.\n"), 1)
        self.assertEqual(errors.count("verify.sh: natedev has been held for 2 h by hana; ask them.\n"), 1)
        self.assertEqual(errors.count("verify.sh: waiting for the Mac (busy) or natedev's release.\n"), 1)
        self.assertEqual(errors.count("claim diagnostic: busy\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertNotIn("verify.sh: running on the Mac", output + errors)
        attempts = self.runner_events()
        self.assertEqual(len(attempts), 5)
        self.assertNotIn("--last-decline", attempts[0]["args"])
        for event in attempts[1:]:
            arguments = event["args"]
            self.assertEqual(arguments[arguments.index("--last-decline") + 1], "busy")

    def test_routing_wait_shows_changed_holder_and_decline_reason_once(self) -> None:
        _ = self.routing_fixture()
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_routing(step_sleep=True)
        with self.hold_environment():
            holders = Path(self.environment["BUILD_HOLD_DIR"])
            _ = build_hold.start_hold(holders, build_hold.HolderIdentity("julian", "julian"), "restore CI", build_hold.NoReleaseEta(), datetime.now().astimezone())
            (holders / "hana").unlink()
        self.resume_routing_sleep(1)
        self.resume_routing_sleep(2)
        _ = (self.runner_state / "busy-reason").write_text("blocked\n")
        self.resume_routing_sleep(3)
        self.resume_routing_sleep(4)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-5").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        for line in ("verify.sh: natedev is held by hana for render checks.\n",
                     "verify.sh: natedev is held by julian for restore CI.\n",
                     "verify.sh: waiting for the Mac (busy) or natedev's release.\n",
                     "verify.sh: waiting for the Mac (blocked) or natedev's release.\n"):
            self.assertEqual(errors.count(line), 1, output + errors)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (blocked)\n"), 1)
        attempts = self.runner_events()
        self.assertEqual([event["args"][event["args"].index("--last-decline") + 1] for event in attempts[1:]],
                         ["busy", "busy", "busy", "blocked", "blocked"])

    def test_hold_wait_includes_declined_retry_seconds_and_excludes_running_retry(self) -> None:
        _ = self.routing_fixture()
        clock = self.control_routing_clock()
        self.environment.update({"TEST_DECLINED_RETRY_SECONDS": "4.5", "TEST_RUNNER_SECONDS": "20",
                                 "TEST_FREE_NOW_FREE": "1"})
        process = self.start_routing(step_sleep=True)
        _ = clock.write_text("102")
        self.resume_routing_sleep(1)
        self.wait_for_path(self.runner_state / "routing-sleep-2")
        _ = clock.write_text("105")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-2").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 3)
        self.assertEqual(self.routing_record()["hold_wait_s"], 2 + 4.5 + 3)
        self.assertEqual(self.last_call()["mac_s"], 20)

    def test_busy_retry_to_blocked_or_malformed_answer_clears_call_holder(self) -> None:
        for mode, reason, expected in (("declined", "blocked", "blocked"), ("none", "", "runner")):
            fixture = VerifyMacOffloadTests("runTest")
            try:
                fixture.setUp()
                _ = fixture.routing_fixture()
                process = fixture.start_routing(pause_retry=True)
                # start_routing sets a successful final answer; select this case's answer here.
                fixture.write_executable(fixture.runner, RUNNER_STUB.replace(
                    'mode = os.environ.get("TEST_RUNNER_MAC", "declined")', f'mode = "{mode}"').replace(
                    'os.environ.get("TEST_RUNNER_REASON", "")', f'"{reason}"'))
                _ = (fixture.runner_state / "mac-free").write_text("free\n")
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                fixture.wait_for_path(fixture.runner_state / "retry-running")
                with fixture.hold_environment():
                    build_hold.holder_path(build_hold.holder_directory(), "hana").unlink()
                _ = (fixture.runner_state / "continue-retry").write_text("continue\n")
                _ = (fixture.runner_state / "continue-sleep-2").write_text("continue\n")
                output, errors = process.communicate(timeout=HANG_GUARD_S)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(fixture.last_call()["mac_reason"], expected)
                self.assertEqual(fixture.last_call()["mac_holder"], "")
            finally:
                fixture.doCleanups()

    def test_busy_mac_and_held_local_wait_then_use_first_free_mac(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("waiting", errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)

        self.assert_no_pending_calls(cycle)
        commands = [event["command"] for event in self.routing_events()]
        self.assertNotIn("finish-local", commands)
        registrations = [event for event in self.routing_events() if event["command"] == "register-local"]
        self.assertTrue(registrations)
        self.assertTrue(all(event["pid"] == process.pid for event in registrations))
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)
        self.assertEqual(self.last_call()["mac_holder"], "")

    def test_local_release_wins_before_another_mac_attempt(self) -> None:
        for released_by_command in (False, True):
            with self.subTest(released_by_command=released_by_command):
                # This test owns a separate fixture for each release variant.
                fixture = VerifyMacOffloadTests("runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                cycle = fixture.routing_fixture()
                process = fixture.start_routing()
                if released_by_command:
                    fixture.release_local_hold()
                else:
                    (Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").unlink()
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                output, errors = process.communicate(timeout=HANG_GUARD_S)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(len(fixture.runner_events()), 1)
                self.assertEqual(len(fixture.local_runs()), 1)
                self.assertFalse((Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())
                fixture.assert_no_pending_calls(cycle)
                self.assertEqual(fixture.routing_record()["target"], "natedev")

    def test_blocked_mac_and_held_local_wait_for_local_release(self) -> None:
        cycle = self.routing_fixture()
        # Exercise the real event waiter. A retry cannot rescue a missed release:
        # its interval is longer than the process hang guard, and a fixed sleep
        # is an immediate fixture error instead of a wall-clock assertion.
        self.environment["BUILD_TARGETS_POLL_S"] = "3600"
        self.write_executable(self.bin_directory / "sleep", 'raise SystemExit("routing must wake on release, not sleep")\n')
        self.write_executable(Path(self.environment["VERIFY_LOCAL_RELEASE_WAIT_SCRIPT"]), f'''
import os
from pathlib import Path
import sys
sys.path.insert(0, {str(VERIFY.parent)!r})
import local_release_wait
original_wait = local_release_wait.LocalReleaseEvents.wait
def observe_wait(self, seconds):
    (Path(os.environ["TEST_RUNNER_STATE"]) / "routing-sleep").write_text("watching\\n")
    original_wait(self, seconds)
local_release_wait.LocalReleaseEvents.wait = observe_wait
local_release_wait.wait_for_local_release(float(sys.argv[1]), sys.argv[2])
''')
        process = self.start_routing("blocked")
        self.release_local_hold()
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("blocked", errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(len(self.local_runs()), 1)
        self.assert_no_pending_calls(cycle)

    def test_local_release_before_routing_watcher_starts_needs_no_retry_wait(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "already-released"
        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            self.release_local_hold()
            with mock.patch.object(local_release_wait.LocalReleaseEvents, "wait",
                                   side_effect=AssertionError("an admitted call must not wait for another event")):
                local_release_wait.wait_for_local_release(3600, call_id)
        self.assert_no_pending_calls(cycle)

    def test_routing_without_inotify_polls_until_local_release(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "polling-routing"
        def release_on_poll(_seconds: float) -> None:
            self.release_local_hold()

        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            with mock.patch.object(ctypes, "CDLL", return_value=object()), \
                 mock.patch.object(time, "monotonic", return_value=100), \
                 mock.patch.object(time, "sleep", side_effect=release_on_poll) as polling:
                local_release_wait.wait_for_local_release(15, call_id)
            polling.assert_called_once_with(15)
            self.assertFalse((Path(self.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        self.assert_no_pending_calls(cycle)

    def test_mac_run_withdraws_local_demand_before_another_waiter_is_released(self) -> None:
        cycle_path = self.routing_fixture()
        process = self.start_routing(pause_retry=True)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        self.assert_no_pending_calls(cycle_path)
        self.release_local_hold()
        self.assertFalse((Path(self.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        self.assertIsNone(process.poll())

        _ = (self.runner_state / "continue-retry").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])

    def test_interrupted_mac_retry_withdraws_registration_and_preserves_only_wait_time(self) -> None:
        cycle_path = self.routing_fixture()
        clock = self.control_routing_clock()
        process = self.start_routing(pause_retry=True)
        _ = clock.write_text("102")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        _ = clock.write_text("500")
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle_path)
        record = self.routing_record()
        self.assertEqual(record["hold_wait_s"], 2)
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(self.last_call()["mac_holder"], "other tests")

    def test_released_hold_keeps_only_routing_wait_seconds(self) -> None:
        _ = self.routing_fixture()
        clock = self.control_routing_clock()
        process = self.start_routing()
        _ = clock.write_text("103")
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        record = self.routing_record()
        self.assertEqual(record["hold_wait_s"], 3)
        self.assertEqual(record["mem_wait_s"], "0")
        self.assertFalse((self.runner_state / "local-wait-added").exists())

    def test_default_routing_retry_is_paced_fifteen_seconds(self) -> None:
        _ = self.routing_fixture()
        _ = self.environment.pop("BUILD_TARGETS_POLL_S")
        process = self.start_routing()
        self.assertEqual((self.runner_state / "sleep-events").read_text().splitlines(), ["15"])
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)

    def test_interrupted_routing_sleep_records_wait_and_cleans_pending_call(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle)
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(list((self.root / "tmp").glob("verify-hold.*")), [])

    def test_free_mac_wins_when_local_hold_was_released_in_the_same_pass(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        self.release_local_hold()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")

    def wait_until(self, condition: Callable[[], bool], what: str,
                   processes: Sequence[subprocess.Popen[str]] = ()) -> None:
        deadline = time.monotonic() + HANG_GUARD_S
        while time.monotonic() < deadline:
            if condition():
                return
            for process in processes:
                if process.poll() is not None:
                    output, errors = process.communicate(timeout=HANG_GUARD_S)
                    self.fail(f"verify exited {process.returncode} before {what}: {output + errors}")
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    def hold_memory(self) -> Path:
        """Keep natedev's memory gate holding until the returned meminfo is rewritten."""
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n", encoding="utf-8")
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        return meminfo

    def leave_once(self) -> dict[str, str]:
        once = self.runner_state / "free-once"
        _ = once.write_text("free\n")
        return {"TEST_FREE_NOW_ONCE": str(once)}

    def free_now_settings(self) -> list[str]:
        path = self.runner_state / "free-now-events"
        return path.read_text().splitlines() if path.exists() else []

    def free_now_roots(self) -> list[str]:
        path = self.runner_state / "free-now-roots"
        return path.read_text().splitlines() if path.exists() else []

    def test_queued_local_test_leaves_for_a_freed_mac_and_ends_with_its_failure(self) -> None:
        _ = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        self.assertEqual(len(self.runner_events()), 1)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertIn("another machine is free after", output)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 2)
        # The step gave the token back for its memory wait and left for the Mac
        # without taking it again.
        self.assertFalse(runners[1]["token_held"])
        arguments = runners[1]["args"]
        self.assertEqual(arguments[arguments.index("--last-decline") + 1], "busy")
        self.assertEqual(set(self.free_now_settings()), {self.environment["MAC_TEST_CONFIG"]})
        self.assertEqual(set(self.free_now_roots()), {f"--repo-root {self.root}"})
        self.assertEqual(self.routing_record()["target"], "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"], record["mac_s"]),
                         (42, "failed", "", 3.25))

    def test_a_step_that_left_for_the_mac_ends_without_the_token_a_peer_now_holds(self) -> None:
        _ = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        # The step gave the token back for its memory wait, and a peer takes it.
        lock = self.board / "locks" / "cargo.d"
        self.assertFalse(lock.exists())
        peer = subprocess.Popen(["sleep", "600"])

        def stop_peer() -> None:
            peer.kill()
            _ = peer.wait(timeout=5)

        self.addCleanup(stop_peer)
        acquired = subprocess.run(["bash", str(VERIFY.with_name("board.sh")), "acquire", str(self.board),
                                   "peer", "cargo", "--pid", str(peer.pid)],
                                  capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac"], "passed_full")
        self.assertEqual((lock / "holder_pid").read_text(), str(peer.pid))

    def test_hold_that_lands_during_the_memory_wait_still_leaves_for_a_freed_mac(self) -> None:
        meminfo = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        with self.hold_environment():
            _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]), build_hold.HolderIdentity("hana", "hana"),
                                      "render checks", build_hold.NoReleaseEta(), datetime.now().astimezone())
            cycle = build_hold.read_cycle()
            assert cycle is not None
        cycle_path = self.root / "release" / cycle["id"] / "cycle.json"

        def waiting_on_the_hold() -> bool:
            return any(Path(self.environment["TMPDIR"]).glob("verify-hold.*/waiter"))

        # Memory frees while natedev is held: the step moves to the hold wait,
        # where the Mac is still busy.
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        self.wait_until(waiting_on_the_hold, "the hold wait", (process,))
        self.assertEqual(len(self.runner_events()), 1)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertIn("leaving natedev's hold wait to run there", output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)
        self.assert_no_pending_calls(cycle_path)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "failed")

    def test_mac_busy_again_after_leaving_returns_to_the_local_wait(self) -> None:
        meminfo = self.hold_memory()
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"), **self.leave_once()})
        self.addCleanup(self.stop_verify, process)
        # The Mac claim declined again; the local run waits with the hook still set.
        self.wait_for_path(self.runner_state / "free-now-after-leave", (process,))
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(output.count("verify.sh: running on natedev"), 2)
        self.assertEqual(self.routing_record()["target"], "natedev")
        record = self.last_call()
        self.assertEqual((record["mac"], record["mac_reason"]), ("declined", "busy"))

    def test_leaving_memory_wait_after_release_runs_on_the_mac(self) -> None:
        cycle = self.routing_fixture()
        _ = self.hold_memory()
        once = self.runner_state / "free-once"
        process = self.start_routing(once=once)
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        # Routing asks free-now once after its sleep; later calls are the hook's.
        self.wait_until(lambda: len(self.free_now_settings()) >= 2, "the leave hook", (process,))
        self.assertEqual(self.local_runs(), [])
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = once.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 2)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")

    def test_mac_state_and_probe_diagnostics_survive_local_fallback(self) -> None:
        for reason, diagnostic in (("state", "mac state cannot be read: invalid block"),
                                   ("blocked", "retained expired block; waiting for removal"),
                                   ("unreachable", "ssh probe failed")):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_REASON": reason, "TEST_RUNNER_OUTPUT": diagnostic})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.assertEqual(self.last_call()["mac_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")

    def test_decline_runs_same_nextest_words_locally_after_runner(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--filter",
            "beta",
            "--features",
            "extra",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "busy",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": "runner output",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runner output", result.stdout)
        runners = self.runner_events()
        local = self.local_runs()
        self.assertEqual(len(runners), 1)
        self.assertEqual(len(local), 1)
        runner_arguments = runners[0]["args"]
        self.assertEqual(runner_arguments[0], "run")
        self.assertEqual(local[0]["args"][2], "--test-threads")
        self.assertGreater(int(local[0]["args"][3]), 0)
        self.assertEqual(
            runner_arguments[runner_arguments.index("--") + 1 :],
            local[0]["args"][4:],
        )
        self.assertIn("--filter-run", runner_arguments)
        self.assertNotIn("--no-cache", runner_arguments)
        self.assertFalse(runners[0]["token_held"])
        self.assertTrue(local[0]["token_held"])
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "busy")
        self.assertEqual(record["mac_holder"], "other tests")
        self.assertEqual(record["mac_s"], 3.25)
        self.assertNotIn("--no-cache", record["command"])

    def test_busy_claim_what_reaches_call_holder_through_the_real_offload_result(self) -> None:
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir()
        record: BusyClaimRecord = {"pid": os.getpid(), "proc_start": build_hold.process_start_time(os.getpid()),
                  "what": "render checks", "worktree": str(self.root),
                  "since": datetime.now(timezone.utc).isoformat()}
        _ = (state / "run.json").write_text(json.dumps(record))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            "VERIFY_MAC_RUNNER": str(VERIFY.parent.parent / "mac_test" / "offload.py")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("busy: render checks since", result.stderr)
        self.assertEqual(self.last_call()["mac_holder"], "render checks")
        self.assertEqual(self.last_call()["mac_reason"], "busy")

    def test_no_test_match_runs_locally_and_reaches_call_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "no_tests",
                "TEST_RUNNER_SECONDS": "4.5",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": (
                    "mac_test: no test matched on the Mac; "
                    "running on natedev instead"
                ),
            },
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "mac_test: no test matched on the Mac; running on natedev instead",
            result.stdout,
        )
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "no_tests")
        self.assertEqual(record["mac_s"], 4.5)

    def test_runner_result_uses_fixture_temp_directory(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "declined", "TEST_RUNNER_STATUS": "75"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runner = self.runner_events()[0]
        result_index = runner["args"].index("--result") + 1
        self.assertTrue(
            Path(runner["args"][result_index]).is_relative_to(self.root / "tmp"),
            runner,
        )

    def test_lost_run_continues_locally(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "lost", "TEST_RUNNER_STATUS": "75"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "lost")
        self.assertEqual(record["mac_s"], 3.25)

    def set_mac_answer(self, mode: str, reason: str, status: int) -> None:
        answer = self.runner_state / "runner-answer"
        temporary = answer.with_suffix(".new")
        _ = temporary.write_text(f"{mode}\n{reason}\n{status}\n")
        _ = temporary.replace(answer)

    def wait_for_retry_probe(self, clock: int, process: subprocess.Popen[str]) -> None:
        probes = self.runner_state / "retry-probes"
        self.wait_until(lambda: probes.exists() and str(clock) in probes.read_text().splitlines(),
                        f"retry probe at {clock}", (process,))

    def check_temporary_mac_decline(self, wait_kind: str, mode: str, reason: str,
                                    *, repeat_decline: bool = True) -> None:
        clock = self.control_routing_clock()
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        if wait_kind == "routing":
            _ = self.routing_fixture()
        elif wait_kind == "first_hold":
            _ = self.routing_fixture()
            record = self.root / "hold-after-routing.json"
            _ = (Path(self.environment["BUILD_HOLD_DIR"]) / "hana").replace(record)
            shell = self.root / "hold-after-routing.sh"
            _ = shell.write_text('''
hold_after_routing() {
    if [[ "$BASH_COMMAND" == route_test ]]; then
        test_routed=1
    elif [[ "${test_routed:-0}" == 1 ]]; then
        test_routed=0
        cp "$TEST_HOLD_RECORD" "$BUILD_HOLD_DIR/hana"
    fi
}
trap hold_after_routing DEBUG
''')
            self.environment.update({"BASH_ENV": str(shell), "TEST_HOLD_RECORD": str(record)})
        else:
            meminfo = self.hold_memory()
        self.set_mac_answer(mode, reason, 75)
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_retry_probe(100, process)
        self.assertEqual(len(self.runner_events()), 1)

        if wait_kind == "hold":
            wrapper = self.root / "targets-wrapper.py"
            self.write_executable(wrapper, TARGETS_WRAPPER)
            # The child already has this wrapper path from its environment.
            with self.hold_environment():
                _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]),
                    build_hold.HolderIdentity("hana", "hana"), "render checks",
                    build_hold.NoReleaseEta(), datetime.now().astimezone())
            _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
            self.wait_for_path(self.runner_state / "local-wait-args", (process,))
            arguments = cast(list[str], json.loads((self.runner_state / "local-wait-args").read_text()))
            self.assertIn("--leave-hook", arguments)
        elif wait_kind == "first_hold":
            arguments = cast(list[str], json.loads((self.runner_state / "local-wait-args").read_text()))
            self.assertIn("--leave-hook", arguments)

        _ = clock.write_text("279")
        self.wait_for_retry_probe(279, process)
        self.assertEqual(len(self.runner_events()), 1)
        if not repeat_decline:
            self.set_mac_answer("passed", "", 0)
        _ = clock.write_text("280")
        if repeat_decline:
            self.wait_until(lambda: len(self.runner_events()) == 2, "second Mac attempt", (process,))
            probes = self.runner_state / "retry-probes"
            self.wait_until(lambda: probes.read_text().splitlines().count("280") >= 2,
                            "wait after the second Mac attempt", (process,))
            _ = clock.write_text("459")
            self.wait_for_retry_probe(459, process)
            self.assertEqual(len(self.runner_events()), 2)
            self.set_mac_answer("passed", "", 0)
            _ = clock.write_text("460")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 3 if repeat_decline else 2)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(errors.count("waiting 180 s before each retry"), 1)
        self.assertEqual(self.last_call()["mac"], "passed_full")
        for event in self.runner_events()[1:]:
            self.assertNotIn("--last-decline", event["args"])

    def test_lost_mac_run_retries_from_the_memory_wait(self) -> None:
        self.check_temporary_mac_decline("memory", "lost", "")

    def check_idle_marker(self, wait_kind: str) -> None:
        _ = self.control_routing_clock()
        self.environment["MAC_IDLE_STATE_DIR"] = str(self.root / "idle-watch")
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        if wait_kind == "routing":
            _ = self.routing_fixture()
        elif wait_kind == "memory":
            _ = self.hold_memory()
        else:
            board = VERIFY.with_name("board.sh")
            acquired = subprocess.run(["bash", str(board), "acquire", str(self.board), "peer", "cargo",
                "--pid", str(os.getpid())], env=self.environment, capture_output=True, text=True, timeout=HANG_GUARD_S)
            self.assertEqual(acquired.returncode, 0, acquired.stderr)
        self.set_mac_answer("lost", "", 75)
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_retry_probe(100, process)
        self.assertEqual(len(self.runner_events()), 1)
        paths = list((self.root / "idle-watch/calls").glob("*.json"))
        self.assertEqual(len(paths), 1)
        self.set_mac_answer("passed", "", 0)
        _ = paths[0].with_suffix(".retry").write_text("retry_delayed\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac"], "passed_full")
        self.assertFalse(paths[0].with_suffix(".retry").exists())
        self.assertEqual(list((self.board / "token_waiters").glob("*")), [])

    def test_idle_marker_bypasses_memory_retry_spacing(self) -> None:
        self.check_idle_marker("memory")

    def test_idle_marker_bypasses_routing_retry_spacing(self) -> None:
        self.check_idle_marker("routing")

    def test_idle_marker_bypasses_token_retry_spacing(self) -> None:
        self.check_idle_marker("token")

    def test_runner_decline_retries_from_the_routing_wait(self) -> None:
        self.check_temporary_mac_decline("routing", "none", "")

    def test_lost_mac_run_retries_from_a_later_hold_wait(self) -> None:
        wrapper = self.root / "targets-wrapper.py"
        self.write_executable(wrapper, TARGETS_WRAPPER)
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(wrapper),
            "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILD_TARGETS_POLL_S": "0.05"})
        self.check_temporary_mac_decline("hold", "lost", "", repeat_decline=False)

    def test_lost_mac_run_retries_when_a_hold_starts_after_routing(self) -> None:
        self.check_temporary_mac_decline("first_hold", "lost", "", repeat_decline=False)

    def test_excluded_mac_is_never_tried_while_local_memory_waits(self) -> None:
        meminfo = self.hold_memory()
        settings = Path(self.environment["MAC_TEST_CONFIG"])
        _ = settings.write_text(f"offload=on\nlinux_only.{self.root.name}=sample\n")
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "sleep-events", (process,))
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.free_now_settings(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_permanent_mac_decline_has_no_leave_hook(self) -> None:
        meminfo = self.hold_memory()
        self.set_mac_answer("declined", "no_tests", 75)
        clock = self.control_routing_clock()
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "sleep-events", (process,))
        _ = clock.write_text("1000")
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(self.free_now_settings(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_missing_temp_root_is_created_before_verify_runs(self) -> None:
        missing = self.root / "new-session" / "tmp"
        result = self.verify("test", "sample", "--no-cache", extra={"TMPDIR": str(missing)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(missing.is_dir())
        self.assertEqual(len(self.local_runs()), 1)
        arguments = self.runner_events()[0]["args"]
        self.assertEqual(Path(arguments[arguments.index("--result") + 1]).parent, missing)

    def test_lint_entry_points_create_missing_temp_roots(self) -> None:
        reader = self.root / "lint-config.sh"
        _ = reader.write_text("lint_config_enabled() { return 0; }\n")
        lint = VERIFY.parent.parent / "lint"
        for entry in ("invoke.sh", "lint"):
            with self.subTest(entry=entry):
                missing = self.root / entry / "tmp"
                arguments = (["bash", "-c", 'set -euo pipefail; source "$1"; run_once printf "step ran\\n"',
                              "temp-root-test", str(lint / entry)] if entry == "invoke.sh" else
                             ["bash", str(lint / entry), "fmt", "--all", "--check"])
                result = subprocess.run(arguments, cwd=self.root,
                    env={**self.environment, "TMPDIR": str(missing)}, capture_output=True,
                    text=True, check=False, timeout=HANG_GUARD_S)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(missing.is_dir())

    def test_mac_failure_stops_before_token_and_local_run(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        self.assertFalse(runners[0]["token_held"])
        self.assertEqual(self.saved_results(), [])
        record = self.last_call()
        self.assertEqual(record["status"], 42)
        self.assertEqual(record["mac"], "failed")
        self.assertEqual(record["mac_reason"], "")
        self.assertEqual(record["mac_s"], 3.25)

    def test_unusable_runner_results_continue_locally(self) -> None:
        cases = (
            ("none", "75"),
            ("none", "1"),
            ("empty", "75"),
            ("directory", "75"),
            ("other", "75"),
            ("passed", "1"),
            ("failed", "0"),
        )
        for mode, status in cases:
            with self.subTest(mode=mode, status=status):
                self.clear_observations()
                result = self.verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_MAC": mode, "TEST_RUNNER_STATUS": status},
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                record = self.last_call()
                self.assertEqual(record["mac"], "declined")
                self.assertEqual(record["mac_reason"], "runner")
                self.assertIsNone(record["mac_s"])

    def test_full_mac_pass_ends_the_call_with_a_mac_pass_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", result.stdout)
        self.assertEqual(self.local_runs(), [])
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("passed on the Mac", saved[0].read_text())
        self.assertIn("host=mac\n", saved[0].read_text())
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_full", 3.25))

        self.clear_observations()
        again = self.verify("test", "sample", extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("PASS (recorded)", again.stdout)
        self.assertEqual(self.runner_events(), [])

        # --local asks for Linux, so the Mac's pass does not answer it.
        self.clear_observations()
        local = self.verify("test", "sample", "--local", "needs Linux process behavior")
        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
        self.assertNotIn("PASS (recorded)", local.stdout)
        self.assertEqual(len(self.local_runs()), 1)
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertNotIn("host=mac", saved[0].read_text())

    def test_filtered_mac_pass_ends_the_call_with_a_mac_pass_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", result.stdout)
        self.assertEqual(self.local_runs(), [])
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        record = self.last_call()
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["mac"], "passed_filter")
        self.assertEqual(record["mac_s"], 3.25)

    def test_local_flag_requires_a_non_option_reason_before_any_work(self) -> None:
        expected = (
            'verify.sh: --local needs a reason: --local "<why this test cannot pass on '
            'macOS>". Leave it off to run on the Mac.\n'
        )
        cases = (
            ("test", "sample", "--local"),
            ("test", "sample", "--local", ""),
            ("test", "sample", "--local", " \t "),
            ("test", "sample", "--local", "--filter", "alpha"),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                call_count = len(self.call_records())
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, expected)
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(len(self.call_records()), call_count)

    def test_local_flag_refuses_a_mac_reservation_before_any_work(self) -> None:
        reasons = (
            "Mac reserved for demo",
            "RESERVATION: demo has the Mac",
            "showrunner ruling: no Mac claim",
            "Mac held by demo",
            "the Mac is blocked",
            "Mac busy, hold off",
        )

        for reason in reasons:
            with self.subTest(reason=reason):
                self.clear_observations()
                call_count = len(self.call_records())
                result = self.verify("test", "sample", "--local", reason)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr,
                    "verify.sh: --local is only for a test that cannot pass on macOS, not to keep "
                    + f'tests off the Mac ("{reason}"). To reserve the Mac, its holder runs '
                    + "/build:hold hold <why> target mac: tests then skip the Mac by themselves, "
                    + "/build:status shows the hold, and it ends at its expiry. Leave --local off.\n",
                )
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(len(self.call_records()), call_count)

    def test_local_flag_accepts_linux_reasons_that_contain_reservation_letters(self) -> None:
        for reason in ("uses a threshold only Linux reports", "needs Wayland; the household fixture is Linux only"):
            with self.subTest(reason=reason):
                result = self.verify("test", "sample", "--local", reason, "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.clear_observations()

    def test_local_flag_skips_runner_records_reason_and_shares_pass_key(self) -> None:
        first_reason = 'needs Linux path handling with a "quoted" mount'
        first = self.verify(
            "test",
            "sample",
            "--local",
            first_reason,
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        first_record = self.last_call()
        self.assertEqual(first_record["command"], "test sample")
        self.assertEqual(first_record["mac"], "declined")
        self.assertEqual(first_record["mac_reason"], "local_flag")
        self.assertEqual(first_record["local_reason"], first_reason)
        self.assertNotIn(first_reason, first_record["command"])
        saved_results = self.saved_results()
        self.assertEqual(len(saved_results), 1)

        self.clear_observations()
        without_flag = self.verify("test", "sample")
        self.assertEqual(
            without_flag.returncode, 0, without_flag.stdout + without_flag.stderr
        )
        self.assertIn("PASS (recorded)", without_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)

        self.clear_observations()
        second_reason = "requires Linux-only process behavior"
        with_flag = self.verify("test", "sample", "--local", second_reason)
        self.assertEqual(with_flag.returncode, 0, with_flag.stdout + with_flag.stderr)
        self.assertIn("PASS (recorded)", with_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)
        second_record = self.last_call()
        self.assertEqual(second_record["local_reason"], second_reason)
        self.assertEqual(second_record["command"], "test sample")

    def test_local_flag_parses_before_and_after_filter(self) -> None:
        reason = "uses Linux filesystem semantics"
        cases = (
            ("test", "sample", "--local", reason, "--filter", "alpha"),
            ("test", "sample", "--filter", "alpha", "--local", reason),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments, "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                local = self.local_runs()
                self.assertEqual(len(local), 1)
                self.assertNotIn("--local", local[0]["args"])
                self.assertNotIn(reason, local[0]["args"])
                record = self.last_call()
                self.assertEqual(record["command"], "test sample --filter alpha")
                self.assertEqual(record["mac_reason"], "local_flag")
                self.assertEqual(record["local_reason"], reason)

    def test_missing_runner_skips_offload(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"VERIFY_MAC_RUNNER": str(self.root / "absent-runner.py")},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_other_verbs_never_call_runner(self) -> None:
        for arguments in (("check", "sample"), ("lint", "sample"), ("final",)):
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])

    def test_runner_receives_each_forwarded_signal_before_local_work(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_observations()
                process = self.start_verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_HOLD": "1"},
                )
                try:
                    self.wait_for_path(self.runner_state / "runner-signals-ready")
                    process.send_signal(signal_number)
                    output, errors = process.communicate(timeout=HANG_GUARD_S)
                except BaseException:
                    self.stop_verify(process)
                    raise
                self.assertEqual(
                    process.returncode, 128 + signal_number, output + errors
                )
                self.assertEqual(
                    (self.runner_state / "runner-signal").read_text(
                        encoding="utf-8"
                    ),
                    str(signal_number),
                )
                self.assertEqual(self.local_runs(), [])
                runners = self.runner_events()
                self.assertEqual(len(runners), 1)
                self.assertFalse(runners[0]["token_held"])

    def test_signal_before_runner_start_removes_result_without_local_work(self) -> None:
        marker = self.runner_state / "before-runner"
        gate = self.root / "debug-gate.sh"
        _ = gate.write_text(
            "set -T\n"
            + "before_mac_runner() {\n"
            + "    if [[ \"$BASH_COMMAND\" == *'\"$MAC_RUNNER\"'* "
            + "&& \"$BASH_COMMAND\" == *'\"${runner_args[@]}\"'* ]]; then\n"
            + "        trap - DEBUG\n"
            + "        : > \"$TEST_BEFORE_RUNNER_MARKER\"\n"
            + "        while [[ ! -e \"$TEST_CONTINUE_RUNNER\" ]]; do\n"
            + "            sleep 0.01\n"
            + "        done\n"
            + "    fi\n"
            + "}\n"
            + "trap before_mac_runner DEBUG\n",
            encoding="utf-8",
        )
        process = self.start_verify(
            "test",
            "sample",
            "--no-cache",
            extra={
                "BASH_ENV": str(gate),
                "TEST_BEFORE_RUNNER_MARKER": str(marker),
                "TEST_CONTINUE_RUNNER": str(self.runner_state / "continue-runner"),
            },
        )
        try:
            self.wait_for_path(marker)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=HANG_GUARD_S)
        except BaseException:
            self.stop_verify(process)
            raise

        self.assertEqual(
            process.returncode, 128 + signal.SIGTERM, output + errors
        )
        self.assertFalse((self.runner_state / "runner-started").exists())
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertFalse(Path(self.environment["TEST_TOKEN_RECORD"]).exists())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def start_copy_call(self, *words: str, copy: dict[str, str]) -> tuple[subprocess.Popen[str], int]:
        """Start a call the busy Mac sends to natedev, where its step compiles
        until cargo-release; return it with the pid of the Mac copy it starts."""
        process = self.start_verify("test", "sample", *words, "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_CARGO_HOLD": "1", **copy})
        self.addCleanup(self.stop_verify, process)
        started = self.runner_state / "copy-started"
        self.wait_for_path(started, (process,))
        self.assertTrue((self.runner_state / "cargo-compiling").exists())
        self.assertEqual(len(self.runner_events()), 1)
        return process, int(started.read_text(encoding="utf-8"))

    def wait_for_reaped(self, pid: int, processes: Sequence[subprocess.Popen[str]]) -> None:
        # Only the watcher collects its copy, and it reads the copy's result
        # when it does: a gone pid means the watcher has taken the result.
        self.wait_until(lambda: not Path(f"/proc/{pid}").exists(), f"the copy {pid} to be collected", processes)

    def assert_copy_record(self, outcome: str, target: str) -> None:
        record = self.routing_record()
        self.assertEqual((record["copy"], record["copy_count"], record["target"]), (outcome, "1", target))
        self.assertGreaterEqual(float(record["copy_s"]), 0)
        self.assertGreaterEqual(float(record["copy_natedev_s"]), 0)

    def test_a_stopped_call_leaves_no_step_or_copy_running(self) -> None:
        process, copy = self.start_copy_call(copy={"TEST_COPY_WAIT": "1"})
        cargo = int((self.runner_state / "cargo-compiling").read_text())
        # The step runs in its own process group, so a kill of verify's group alone misses it.
        self.assertNotEqual(os.getpgid(cargo), process.pid)
        self.assertTrue({cargo, copy} <= processes_carrying(self.child_marker()))
        # Both folders carry the run's pid, and the copy folder sits in the hold folder, so
        # tmp_sweep.py can tie a folder a kill leaves behind to its run.
        holds = list((self.root / "tmp").glob(f"verify-hold.{process.pid}.*"))
        self.assertEqual(len(holds), 1, list((self.root / "tmp").iterdir()))
        self.assertEqual(len(list(holds[0].glob(f"verify-copy.{process.pid}.*"))), 1)
        self.stop_verify(process)
        self.assertEqual(processes_carrying(self.child_marker()), set())

    def test_copy_failure_first_stops_natedev_and_exits_with_the_copy_status(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "failed", "TEST_COPY_STATUS": "42"})
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        self.assertEqual(self.saved_results(), [])
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"], record["mac_s"]),
                         (42, "failed", "", 2.5))

    def test_copy_filtered_pass_first_stops_natedev_and_records_a_mac_pass(self) -> None:
        process, _ = self.start_copy_call("--filter", "alpha", copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_filter", 2.5))

    def test_copy_full_pass_first_stops_natedev_and_ends_the_call(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_full", 2.5))

    def test_natedev_finishing_first_stops_the_copy_and_its_result_stands(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_WAIT": "1"})
        _ = (self.runner_state / "cargo-release").write_text("release\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual((self.runner_state / "copy-signal").read_text(), str(signal.SIGTERM))
        self.assertFalse((self.runner_state / "cargo-stopped").exists())
        self.assertEqual(len(self.saved_results()), 1)
        self.assert_copy_record("natedev_won", "natedev")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"]), (0, "declined", "busy"))

    def test_new_test_claim_makes_the_copy_give_way_and_natedev_continues(self) -> None:
        mac_test_script = str(VERIFY.parent.parent / "mac_test" / "mac_test.py")
        # No second copy after the first gives way: the next check is an hour off.
        self.environment["BUILD_TARGETS_POLL_S"] = "3600"
        process, copy = self.start_copy_call(copy={"TEST_COPY_WAIT": "1", "TEST_COPY_CLAIM": mac_test_script})

        def claim(*words: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, mac_test_script, *words, "--pid", str(os.getpid())],
                                  env=self.environment, capture_output=True, text=True, check=False,
                                  timeout=HANG_GUARD_S)

        new_test = claim("claim", "--what", "nextest other", "--worktree", str(self.root))
        self.addCleanup(claim, "release")
        self.assertEqual(new_test.returncode, 0, new_test.stdout + new_test.stderr)
        self.assertIn("claimed", new_test.stdout)
        # The claim signals the copy; its handler records the signal some time later on a loaded machine.
        self.wait_for_path(self.runner_state / "copy-signal", (process,))
        self.assertEqual((self.runner_state / "copy-signal").read_text(), str(signal.SIGTERM))
        self.wait_for_reaped(copy, (process,))
        self.assertIsNone(process.poll())
        _ = (self.runner_state / "cargo-release").write_text("release\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertFalse((self.runner_state / "cargo-stopped").exists())
        self.assertEqual(len(self.saved_results()), 1)
        self.assert_copy_record("gave_way", "natedev")
        self.assertEqual(claim("release").returncode, 0)


if __name__ == "__main__":
    _ = unittest.main()
