"""Exercise verify.sh Mac offload routing with disposable command stand-ins."""

from __future__ import annotations

import json
import os
import errno
import select
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
from datetime import datetime
from pathlib import Path
from typing import ClassVar, TypedDict, cast, override
from unittest import mock

from scripts.build_hold import build_hold
from scripts.direct.local_release_wait import LocalReleaseEvents, local_release_waiter
from scripts.production.cli_fixture import invoke_cli


VERIFY = Path(__file__).with_name("verify.sh")
# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300
# Existing fixture consumers import this name.
HANG_GUARD_S = TEST_HANG_GUARD_SECS


def invoke_mac_claim(arguments: list[str], environment: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the real claim CLI; its copy signals still cross the process boundary."""
    with mock.patch.object(sys, "path", [str(VERIFY.parent.parent / "mac_test"), *sys.path]):
        from scripts.mac_test import mac_test
        return invoke_cli(mac_test, mac_test.main, arguments, environment, cwd)


def stub_build_tool_path(directory: Path) -> str:
    """Use private build stubs and direct links to shell tools."""
    for name in (
        "bash", "sh", "python3", "git", "awk", "sed", "grep", "tee", "date",
        "sleep", "mktemp", "dirname", "basename", "cat", "head", "uname", "tr",
        "cut", "find", "xargs", "ps", "kill", "readlink", "stat", "mkdir", "rm",
        "mv", "cp", "sort", "wc", "touch", "env", "chmod", "id", "hostname",
        "flock", "timeout", "seq", "tail", "realpath", "perl", "nproc", "getconf",
        "lscpu", "du", "df", "journalctl", "which", "setsid", "ln", "printf",
        "cmp", "rmdir", "true", "false", "install", "readelf", "file",
        "sha256sum", "shasum",
    ):
        target = directory / name
        source = shutil.which(name)
        if source and not target.exists():
            if name == "sleep":
                # Other verify fixtures replace sleep by writing it directly.
                _ = target.write_text(f'#!/bin/sh\nexec {shlex.quote(source)} "$@"\n')
                target.chmod(0o755)
            else:
                target.symlink_to(source)
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


def wait_for_marker_exit(marker: str, timeout: float) -> set[int]:
    """Wait for every marked process to exit, returning survivors without killing any."""
    deadline = time.monotonic() + timeout
    while children := processes_carrying(marker):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return children
        descriptors: list[int] = []
        poll_exits = False
        try:
            for pid in children:
                try:
                    descriptors.append(os.pidfd_open(pid))
                except ProcessLookupError:
                    pass
                except AttributeError:
                    poll_exits = True
                except OSError as error:
                    if error.errno not in (errno.ENOSYS, errno.ENOTSUP, errno.EPERM):
                        raise
                    poll_exits = True
            if descriptors:
                _ = select.select(descriptors, [], [], min(0.01, remaining) if poll_exits else remaining)
            elif poll_exits:
                time.sleep(min(0.01, remaining))
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
    return set()


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
    for pid in stopped:
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            _ = select.select([descriptor], [], [], max(0, deadline - time.monotonic()))
        finally:
            os.close(descriptor)
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
    sys.path.insert(0, str(Path(os.environ["TEST_WAIT_HELPER"]).resolve().parents[2]))
    from scripts.direct.wait_for_test_state import wait_for_file
    wait_for_file(root / "cargo-release")
    if os.environ.get("TEST_CARGO_STARTING", "1") == "1":
        print("     Starting 1 test", flush=True)
if arguments[:2] == ["nextest", "run"]:
    raise SystemExit(int(os.environ.get("TEST_LOCAL_STATUS", "0")))
raise SystemExit(0)
'''


GIT_STUB = r'''#!/bin/bash
has() { local word; for word in "$@"; do [[ "$word" == "$wanted" ]] && return 0; done; return 1; }
wanted=check-ignore
if has "$@"; then
    case "${!#}" in target/*) exit 0 ;; *) exit 1 ;; esac
fi
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
        sys.path.insert(0, str(Path(os.environ["TEST_WAIT_HELPER"]).resolve().parents[2]))
        from scripts.direct.wait_for_test_state import wait_for_file
        wait_for_file(root / "copy-release")
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
forwarded_names = [arguments[index + 1] for index, word in enumerate(arguments) if word == "--env"]
(root / "forwarded-environment.json").write_text(json.dumps({name: os.environ.get(name) for name in forwarded_names}))

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
    sys.path.insert(0, str(Path(os.environ["TEST_WAIT_HELPER"]).resolve().parents[2]))
    from scripts.direct.wait_for_test_state import wait_for_file
    wait_for_file(root / "continue-retry")

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
        + "seconds=" + os.environ.get("TEST_RUNNER_SECONDS", "3.25") + "\n"
        + ("fetch=" + os.environ["TEST_RUNNER_FETCH"] + "\n" if "TEST_RUNNER_FETCH" in os.environ else ""),
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

sys.path.insert(0, str(Path(os.environ["TEST_REAL_TARGETS"]).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic

command = sys.argv[1]
root = Path(os.environ["TEST_RUNNER_STATE"])
if command == "wait-local":
    write_text_atomic(root / "local-wait-args", json.dumps(sys.argv[2:]))
if command == "wait-local" and os.environ.get("TEST_LOCAL_WAIT_SECONDS") and not (root / "local-wait-added").exists():
    with Path(os.environ["BUILDLOG_HOLD_WAIT_FILE"]).open("a") as output:
        output.write(os.environ["TEST_LOCAL_WAIT_SECONDS"] + "\n")
    write_text_atomic(root / "local-wait-added", "added\n")
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
    write_text_atomic(root / "registered", "registered\n")
raise SystemExit(status)
'''


SLEEP_STUB = r'''#!/bin/sh
# Record the requested interval; only explicit routing gates block this fixture.
root=$TEST_RUNNER_STATE
printf '%s\n' "$1" >> "$root/sleep-events"
gate=""
if [ "${TEST_STEP_ROUTING_SLEEP:-0}" = 1 ]; then
    step=$(wc -l < "$root/sleep-events")
    step=$((step + 0))
    printf 'sleeping\n' > "$root/routing-sleep"
    printf 'sleeping\n' > "$root/routing-sleep-$step"
    gate="$root/continue-sleep-$step"
elif [ "${TEST_PAUSE_ROUTING_SLEEP:-0}" = 1 ]; then
    printf 'sleeping\n' > "$root/routing-sleep"
    gate="$root/continue-sleep"
fi
if [ -f "$root/memory-event.py" ] && [ "${TEST_ROUTING_WAIT:-0}" != 1 ]; then
    exec "$TEST_REAL_PYTHON" "$root/memory-event.py" "$gate"
elif [ -n "$gate" ]; then
    exec "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$gate"
fi
'''


MEMORY_EVENT_STUB = r'''
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(os.environ["TEST_WAIT_HELPER"]).resolve().parents[2]))
from scripts.direct.local_release_wait import local_release_waiter
from scripts.direct.wait_for_test_state import wait_for_file

if len(sys.argv) > 1 and sys.argv[1]:
    wait_for_file(Path(sys.argv[1]))
root = Path(os.environ["TEST_RUNNER_STATE"])
meminfo = Path(os.environ["BUILDLOG_MEMINFO"])
clock = Path(os.environ["TEST_ROUTING_CLOCK"]) if os.environ.get("TEST_ROUTING_CLOCK") else root / "no-clock"
probes = root / "retry-probes"
idle = Path(os.environ["MAC_IDLE_STATE_DIR"]) / "calls"

# A new memory wait announces its first held check before invoking its hook.
# Advance that check once per Mac attempt; later checks need an input event.
attempts = root / "runner-events.jsonl"
checked = root / "memory-checked-attempt"
attempt = str(len(attempts.read_text().splitlines()) if attempts.exists() else 0)
if not checked.exists() or checked.read_text() != attempt:
    checked.write_text(attempt)
    raise SystemExit(0)

def changed():
    fields = meminfo.read_text().split()
    return (len(fields) > 1 and int(fields[1]) > 0
            or (root / "mac-free").exists() or (root / "free-once").exists()
            or any(idle.glob("*.retry"))
            or (clock.exists() and probes.exists() and probes.read_text().splitlines()
                and clock.read_text().strip() != probes.read_text().splitlines()[-1]))

# Subscribe before the predicate; the probe records the clock actually checked,
# so a clock change before this process starts is also visible.
with local_release_waiter(meminfo.parent, idle) as events:
    import select
    import time
    from scripts.direct.local_release_wait import LocalReleaseEvents
    deadline = time.monotonic() + float(os.environ["TEST_HANG_GUARD_SECS"])
    while True:
        events.arm()
        if changed():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("memory fixture was never released")
        if isinstance(events, LocalReleaseEvents):
            readable, _, _ = select.select([events.descriptor], [], [], remaining)
            if readable:
                _ = os.read(events.descriptor, 65536)
        else:
            events.wait(min(0.01, remaining))
'''


# verify invokes this hook through Python; replace its process with the shell
# sleep stand-in so both entry paths share the same event and interval records.
ROUTING_WAIT_STUB = r'''import os
import sys
# This invocation waits for routing release, before memory admission begins.
os.environ["TEST_ROUTING_WAIT"] = "1"
os.execv("/bin/sh", ["sh", os.environ["TEST_ROUTING_SLEEP_SCRIPT"], *sys.argv[1:]])
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

# The routing simulator shares its availability schedule with the new
# claim-wait hook. Real claim locking and transfer are tested separately.
if sys.argv[1] == "release":
    raise SystemExit(0)
if sys.argv[1] == "claim-wait":
    assert sys.argv[-4] == "--pid" and sys.argv[-2] == "--package", sys.argv
    assert int(sys.argv[-3]) > 0, sys.argv
    sys.argv = [sys.argv[0], "free-now", *sys.argv[2:-4]]

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


ROUTING_CLOCK_BASH = r'''
# Only the disposable verify copy uses fixture time. Export the probe because
# local wait helpers evaluate the retry hook in a child Bash.
fixture_routing_clock() {
    local now
    read -r now < "$TEST_ROUTING_CLOCK" || [[ -n "$now" ]]
    printf '%s.000000\n' "$now"
}
fixture_routing_probe() {
    local now
    read -r now < "$TEST_ROUTING_CLOCK" || [[ -n "$now" ]]
    printf '%s\n' "$now" >> "$TEST_RUNNER_STATE/retry-probes"
    printf '%s000000\n' "$now"
}
export -f fixture_routing_clock fixture_routing_probe
'''


class VerifyMacOffloadFixture(unittest.TestCase):
    """Independent disposable tools and state for real verify.sh processes."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.home: Path = Path()
        self.bin_directory: Path = Path()
        self.board: Path = Path()
        self.runner: Path = Path()
        self.runner_state: Path = Path()
        self.cargo_events_path: Path = Path()
        self.environment: dict[str, str] = {}
        self.verify_script: Path = VERIFY

    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="verify-mac-")
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture_root = Path(temporary.name) / "case"
        cls.fixture_seed = Path(temporary.name) / "seed"
        cls.fixture_root.mkdir()
        builder = cls()
        builder.root = cls.fixture_root
        builder.build_fixture()
        cls.fixture_environment = builder.environment.copy()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_seed, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        if "fixture_seed" in type(self).__dict__:
            self.root = self.fixture_root
            _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
            self.addCleanup(shutil.rmtree, self.root)
            self.fixture_paths()
            self.environment = self.fixture_environment.copy()
        else:
            # Standalone consumers own their fixture through doCleanups().
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
            self.build_fixture()
        self.addCleanup(self.assert_nothing_outlives)

    def fixture_paths(self) -> None:
        self.home = self.root / "home"
        self.bin_directory = self.root / "bin"
        self.board = self.root / "board"
        self.runner_state = self.root / "runner-state"
        self.cargo_events_path = self.root / "cargo-events.jsonl"
        self.runner = self.root / "runner.py"

    def build_fixture(self) -> None:
        self.fixture_paths()
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
        self.bin_directory.mkdir()
        self.runner_state.mkdir()
        self.write_executable(self.bin_directory / "cargo", CARGO_STUB)
        self.write_executable(self.bin_directory / "git", GIT_STUB)
        self.write_executable(self.bin_directory / "rustc", RUSTC_STUB)
        self.write_executable(self.bin_directory / "taplo", "#!/bin/sh\nexit 0\n")
        self.write_executable(self.runner, RUNNER_STUB)
        self.write_executable(self.root / "free-now.py", FREE_NOW_STUB)
        blocker = "#!/bin/sh\nexit 99\n"
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.write_executable(self.bin_directory / name, blocker)
        self.write_executable(self.bin_directory / "ssh", '#!/bin/sh\nprintf "free_gib=1000\\nhost=test-mac\\nrc=0\\n"\n')
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
        # Admission must use this test's memory state, including pressure.
        pressure = self.root / "memory-pressure"
        _ = pressure.write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
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
        assertion_guard = self.root / "assertion-guard.py"
        self.write_executable(assertion_guard, "raise SystemExit(0)\n")
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
            # The private toolchain is complete; host Nix discovery is unrelated.
            "IN_NIX_SHELL": "pure",
            "BUILDLOG_BUILD_BUDGET_GIB": "64",
            "PACKAGE_ROOT": str(self.root),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "VERIFY_STEPS_DIR": str(self.root / "verify-steps"),
            "VERIFY_ASSERTION_GUARD_SCRIPT": str(assertion_guard),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_MEMORY_PRESSURE": str(pressure),
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
            "TEST_REAL_PYTHON": sys.executable,
            "TEST_HANG_GUARD_SECS": str(TEST_HANG_GUARD_SECS),
            "TEST_ROUTING_SLEEP_SCRIPT": str(self.bin_directory / "sleep"),
            "TEST_WAIT_HELPER": str(VERIFY.with_name("wait_for_test_state.py")),
            "TEST_CARGO_EVENTS": str(self.cargo_events_path),
            "TEST_TOKEN_RECORD": str(self.board / "locks" / "cargo.d" / "owned"),
            "TEST_METADATA": json.dumps(metadata),
            "TEST_RUNNER_SECONDS": "3.25",
            "TEST_REAL_RECORD": str(VERIFY.parent.parent / "buildlog" / "record.py"),
        }

        cache = self.root / "cache/claude/python3-3.11"
        cache.parent.mkdir(parents=True, exist_ok=True)
        _ = cache.write_text(sys.executable + "\n")
        _ = self.environment.pop("CLAUDE_PYTHON", None)

    def write_executable(self, path: Path, source: str) -> None:
        # Private tools may start as links; replacing one must never edit its target.
        if path.is_symlink():
            path.unlink()
        _ = path.write_text(source if source.startswith("#!") else f"#!{sys.executable}\n" + source,
                            encoding="utf-8")
        path.chmod(0o755)

    def verify(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.verify_script), *arguments],
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
            ["bash", str(self.verify_script), *arguments],
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
        self.wait_until(path.exists, str(path), processes)

    def routing_fixture(self) -> Path:
        wrapper = self.root / "targets-wrapper.py"
        self.write_executable(wrapper, TARGETS_WRAPPER)
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        routing_wait = self.root / "routing-wait.py"
        self.write_executable(routing_wait, ROUTING_WAIT_STUB)
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(wrapper),
                                 "VERIFY_LOCAL_RELEASE_WAIT_SCRIPT": str(routing_wait),
                                 "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets" / "build_targets.py")})
        with self.hold_environment():
            holders = Path(self.environment["BUILD_HOLD_DIR"])
            _ = build_hold.start_hold(holders, build_hold.HolderIdentity("hana", "hana"), "render checks", build_hold.NoReleaseEta(), datetime.now().astimezone())
            cycle = build_hold.read_cycle()
            assert cycle is not None
            return self.root / "release" / cycle["id"] / "cycle.json"

    def control_routing_clock(self) -> Path:
        clock = self.runner_state / "routing-clock"
        _ = clock.write_text("100")
        # Preserve verify's relative helper paths while replacing only its
        # clock expressions; the production script needs no fixture hooks.
        scripts = self.root / "clock-scripts"
        direct = scripts / "direct"
        direct.mkdir(parents=True)
        for sibling in VERIFY.parent.parent.iterdir():
            if sibling.name != "direct":
                (scripts / sibling.name).symlink_to(sibling, target_is_directory=sibling.is_dir())
        for helper in VERIFY.parent.iterdir():
            if helper.name != "verify.sh":
                (direct / helper.name).symlink_to(helper, target_is_directory=helper.is_dir())
        source = VERIFY.read_text()
        source = source.replace("local now=$EPOCHREALTIME", "local now=$(fixture_routing_clock)")
        source = source.replace("ROUTING_WAIT_STARTED=$EPOCHREALTIME", "ROUTING_WAIT_STARTED=$(fixture_routing_clock)")
        source = source.replace("${EPOCHREALTIME/./}", "$(fixture_routing_probe)")
        # Fixture time already represents monotonic seconds, so its offset is zero.
        source = source.replace(" - VERIFY_MONOTONIC_OFFSET_US", "")
        source = source.replace('"$VERIFY_MONOTONIC_OFFSET_US" "${deadline/./}"', '0 "${deadline/./}"')
        self.verify_script = direct / "verify.sh"
        _ = self.verify_script.write_text(source.replace("set -euo pipefail", ROUTING_CLOCK_BASH + "\nset -euo pipefail", 1))
        self.environment["TEST_ROUTING_CLOCK"] = str(clock)
        return clock

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
        # Lint recorders intentionally detach from their caller. Wait for their
        # exit events; a two-second grace period races their work under load.
        _ = wait_for_marker_exit(self.child_marker(), HANG_GUARD_S)
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

    def resume_routing_sleep(self, step: int) -> None:
        _ = (self.runner_state / f"continue-sleep-{step}").write_text("continue\n")
        self.wait_for_path(self.runner_state / f"routing-sleep-{step + 1}")

    def wait_until(self, condition: Callable[[], bool], what: str,
                   processes: Sequence[subprocess.Popen[str]] = (), *, directory: Path | None = None) -> None:
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        descriptors: list[int] = []
        try:
            for process in processes:
                try:
                    descriptors.append(os.pidfd_open(process.pid))
                except ProcessLookupError:
                    pass
            with local_release_waiter(directory or self.root, self.root / "tmp") as events:
                if isinstance(events, LocalReleaseEvents):
                    add_watch = events.add_watch
                    def watch_modifications(fd: int, path: bytes, mask: int) -> int:
                        return add_watch(fd, path, mask | 0x2)

                    events.add_watch = watch_modifications
                while True:
                    events.arm()
                    if condition():
                        return
                    for process in processes:
                        if process.poll() is not None:
                            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                            self.fail(f"verify exited {process.returncode} before {what}: {output + errors}")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.fail(f"timed out waiting for {what}")
                    if isinstance(events, LocalReleaseEvents):
                        readable, _, _ = select.select([events.descriptor, *descriptors], [], [], remaining)
                        if events.descriptor in readable:
                            # Fixture markers are arbitrary names, including open logs;
                            # consume every directory event, not just hold-state names.
                            _ = os.read(events.descriptor, 65536)
                    else:
                        events.wait(min(0.01, remaining))
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    def hold_memory(self) -> Path:
        """Keep natedev's memory gate holding until the returned meminfo is rewritten."""
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n", encoding="utf-8")
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        self.write_executable(self.runner_state / "memory-event.py", MEMORY_EVENT_STUB)
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
        # After collecting a yielded copy, the watcher publishes its next
        # candidate. Watch that directory so PID collection wakes the predicate.
        self.wait_until(lambda: not Path(f"/proc/{pid}").exists(), f"the copy {pid} to be collected", processes,
                        directory=Path(self.environment["MAC_TEST_STATE_DIR"]) / "copy_candidates")

    def assert_copy_record(self, outcome: str, target: str) -> None:
        record = self.routing_record()
        self.assertEqual((record["copy"], record["copy_count"], record["target"]), (outcome, "1", target))
        self.assertGreaterEqual(float(record["copy_s"]), 0)
        self.assertGreaterEqual(float(record["copy_natedev_s"]), 0)

