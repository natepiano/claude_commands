"""Exercise verify.sh Mac offload routing with disposable command stand-ins."""

from __future__ import annotations

from scripts.build_queue.admission_test_support import caller_admission, caller_provider_controls, private_provider_evidence
from scripts.build_queue import admission_protocol as wire, admission_service as service, placement
from scripts.mac_test import claim_state
from scripts.lib.py_floor_test_support import seed_interpreter_cache

from scripts.copies.copies_test_support import copies_environment
from scripts.lint.memory_test_support import config_environment, write_config_fixture, write_pressure_reading

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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, NotRequired, TypedDict, cast, override
from unittest import mock

from scripts.build_hold import build_hold
from scripts.direct.local_release_wait import RELEASE_CHANGES, LocalReleaseEvents, local_release_waiter
from scripts.lib import inotify
from scripts.production.cli_fixture import invoke_cli
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.shell_test_support import shell_functions


VERIFY = Path(__file__).with_name("verify.sh")


def invoke_mac_claim(arguments: list[str], environment: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the real claim CLI; its copy signals still cross the process boundary."""
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
        "cmp", "rmdir", "true", "false", "install", "readelf", "file", "sha256sum", "shasum",
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
    """Wait for marked processes to exit. Return survivors without a signal."""
    deadline = time.monotonic() + timeout
    while children := processes_carrying(marker):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
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
    return processes_carrying(marker)


def reap(marker: str) -> dict[int, str]:
    """Kill every process carrying `marker` and wait until none is left.

    verify.sh runs each step in its own process group and its step owner in
    its own session, so a kill of verify's group leaves the step's tree
    running under init. A stopped process cannot fork or exit, so stopping
    each one found until a scan finds no new one catches the whole tree.
    """
    stopped: dict[int, str] = {}
    while fresh := processes_carrying(marker) - stopped.keys():
        for pid in fresh:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGSTOP)
            try:
                arguments = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
                stopped[pid] = shlex.join(os.fsdecode(word) for word in arguments if word)
            except OSError as error:
                stopped[pid] = f"command unavailable: {error}"
    for pid in stopped:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + TEST_HANG_GUARD_SECS
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


@dataclass(frozen=True)
class HoldRecorderStart:
    """Track a recorder thread until its start call finishes."""

    thread: threading.Thread
    finished: threading.Event


@contextmanager
def hold_recorder_exits(timeout: float = TEST_HANG_GUARD_SECS) -> Generator[None, None, None]:
    """Wait for hold recorders before their fixture environment is restored."""
    children: list[subprocess.Popen[bytes]] = []
    starts: list[HoldRecorderStart] = []
    launch = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)
    start = threading.Thread.start

    def launch_owned(*arguments: object, **options: object) -> subprocess.Popen[bytes]:
        process = launch(*arguments, **options)
        children.append(process)
        return process

    def start_owned(thread: threading.Thread) -> None:
        if thread.name != "hold-event-recorder":
            start(thread)
            return
        recorder = HoldRecorderStart(thread, threading.Event())
        starts.append(recorder)
        try:
            start(thread)
        finally:
            recorder.finished.set()

    with mock.patch.object(subprocess, "Popen", side_effect=launch_owned), \
         mock.patch.object(threading.Thread, "start", start_owned):
        try:
            yield
        finally:
            timed_out = False
            for recorder in starts:
                deadline = time.monotonic() + timeout
                if not recorder.finished.wait(timeout=max(0, deadline - time.monotonic())):
                    timed_out = True
                    continue
                if recorder.thread.ident is None:
                    timed_out = True
                    continue
                recorder.thread.join(timeout=max(0, deadline - time.monotonic()))
                timed_out |= recorder.thread.is_alive()
            if timed_out:
                for process in children:
                    if process.poll() is None:
                        process.terminate()
                for process in children:
                    if process.poll() is None:
                        process.kill()
                    _ = process.wait()
                for recorder in starts:
                    if recorder.thread.ident is not None:
                        recorder.thread.join()
                raise AssertionError("fixture hold recorder never exited")


class CargoEvent(TypedDict):
    args: list[str]
    token_held: bool
    token_holder: NotRequired[str]
    coordinator: NotRequired[str]


class RunnerEvent(TypedDict):
    args: list[str]
    token_held: bool
    at: float
    clock: NotRequired[str]


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
    claim_id: str
    episode: str
    attempt: str
    submitting_session: str
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
if tuple(arguments) in (("mend", "--version"), ("+nightly", "fmt", "--version"), ("clippy", "--version")):
    print("cargo fixture 1.0")
    raise SystemExit(0)
event = {
    "args": arguments,
    "token_holder": Path(os.environ["TEST_TOKEN_RECORD"]).read_text().strip() if Path(os.environ["TEST_TOKEN_RECORD"]).exists() else "",
    "coordinator": os.environ.get("BUILDLOG_HOLD_PID", ""),
    "token_held": (Path(os.environ["TEST_TOKEN_RECORD"]).exists()
                   and Path(os.environ["TEST_TOKEN_RECORD"]).read_text().strip() == os.environ.get("BUILDLOG_HOLD_PID", "")),
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
    with open(Path(os.environ["TEST_WAIT_HELPER"]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.wait_for_test_state import wait_for_file
    wait_for_file(root / "cargo-release")
    if os.environ.get("TEST_CARGO_STARTING", "1") == "1":
        print("     Starting 1 test", flush=True)
if arguments[:2] == ["nextest", "run"]:
    raise SystemExit(int(os.environ.get("TEST_LOCAL_STATUS", "0")))
raise SystemExit(0)
'''


def install_fixed_cargo(path: Path) -> None:
    """Install shell replies for an unchanged Python Cargo stub."""
    python_source = f"#!{sys.executable}\n" + CARGO_STUB
    if path.read_text(encoding="utf-8") != python_source:
        return
    fallback = path.with_name(path.name + "-python")
    _ = fallback.write_text(python_source, encoding="utf-8")
    fallback.chmod(0o755)
    script = r'''#!/bin/sh
python_reply() {
    exec PYTHON FALLBACK "$@"
}
if [ "$#" = 2 ]; then
    case "$1:$2" in
        mend:--version|clippy:--version) printf 'cargo fixture 1.0\n'; exit 0 ;;
    esac
fi
if [ "$#" = 3 ] && [ "$1:$2:$3" = '+nightly:fmt:--version' ]; then
    printf 'cargo fixture 1.0\n'
    exit 0
fi
if [ "${1:-}:${2:-}" = nextest:run ] && [ "${TEST_CARGO_HOLD:-}" = 1 ]; then
    python_reply "$@"
fi
token_exists=0
token_holder=''
if [ -e "$TEST_TOKEN_RECORD" ]; then
    token_exists=1
    read -r token_holder < "$TEST_TOKEN_RECORD" || true
fi
coordinator=${BUILDLOG_HOLD_PID-}
original_locale=${LC_ALL-}
locale_was_set=${LC_ALL+x}
LC_ALL=C
for argument in "$@" "$token_holder" "$coordinator"; do
    case "$argument" in
        *[!\ -~]*)
            if [ "$locale_was_set" = x ]; then LC_ALL=$original_locale; else unset LC_ALL; fi
            python_reply "$@"
            ;;
    esac
done
json_quote() {
    argument=$1
    quoted=''
    while [ -n "$argument" ]; do
        remaining=${argument#?}
        character=${argument%"$remaining"}
        case "$character" in
            '"'|'\') quoted=$quoted\\$character ;;
            *) quoted=$quoted$character ;;
        esac
        argument=$remaining
    done
}
arguments=''
separator=''
for argument do
    json_quote "$argument"
    arguments=$arguments$separator\"$quoted\"
    separator=', '
done
json_quote "$token_holder"
token_json=$quoted
json_quote "$coordinator"
coordinator_json=$quoted
token_held=false
if [ "$token_exists" = 1 ] && [ "$token_holder" = "$coordinator" ]; then token_held=true; fi
printf '{"args": [%s], "token_holder": "%s", "coordinator": "%s", "token_held": %s}\n' \
    "$arguments" "$token_json" "$coordinator_json" "$token_held" >> "$TEST_CARGO_EVENTS" || exit 1
if [ "${1:-}" = metadata ]; then
    printf '%s\n' "$TEST_METADATA"
    exit 0
fi
if [ "${1:-}:${2:-}" = nextest:run ]; then exit "${TEST_LOCAL_STATUS:-0}"; fi
exit 0
'''
    _ = path.write_text(script.replace("PYTHON FALLBACK", f"{shlex.quote(sys.executable)} {shlex.quote(str(fallback))}"),
                        encoding="utf-8")
    path.chmod(0o755)


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
wanted=ls-files
if has "$@"; then
    wanted=--others
    if ! has "$@"; then printf 'Cargo.toml\0'; fi
    exit 0
fi
for wanted in status diff ls-tree; do if has "$@"; then exit 0; fi; done
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


ASSIGNED_RUNNER_SETUP = r'''
if "--assignment-file" in arguments:
    import atexit
    import io
    import uuid
    from contextlib import redirect_stdout

    with open(Path(os.environ["TEST_REAL_RECORD"]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
    from scripts.build_queue import admission_client, admission_protocol as wire, placement
    from scripts.mac_test import mac_test as assigned_mac_test, claim_state
    from scripts.lib import process_start

    assignment = admission_client.read_assignment(Path(arguments[arguments.index("--assignment-file") + 1])).assignment
    claim_args = assigned_mac_test.CliArguments()
    claim_args.pid, claim_args.worktree = os.getpid(), arguments[arguments.index("--repo-root") + 1]
    claim_args.target, claim_args.remote = assignment.destination.target, assignment.destination.host
    claim_args.what, claim_args.copy = "nextest sample", "--copy" in arguments
    if "--claim-owner-pid" in arguments:
        claim_args.from_pid = int(arguments[arguments.index("--claim-owner-pid") + 1])
    for field in ("claim_id", "episode", "attempt"):
        flag = "--" + field.replace("_", "-")
        if flag in arguments:
            setattr(claim_args, field, arguments[arguments.index(flag) + 1])
    paths = claim_state.state_paths(claim_args.target)
    with redirect_stdout(io.StringIO()):
        claim_status = assigned_mac_test.command_claim(claim_args, paths, watch_claim=False,
            session=assignment.session, assignment=assignment)
    if claim_status != 0:
        raise SystemExit(75)
    held, = [held for held in claim_state.read_claims(paths) if held.record["pid"] == os.getpid()]
    identity = claim_state.claim_identity(held.record)
    reference = placement.ProviderClaimReference(assignment.destination.provider, assignment.destination.target,
        assignment.destination.scope, identity.claim_id, identity.episode, identity.attempt)
    start = process_start.process_start(os.getpid())
    assert isinstance(start, process_start.Started)
    holder = placement.ClaimHolderIdentity(assignment.owner.host, os.getpid(), start.start)
    permission_deadline = time.monotonic() + 10
    while True:
        permission = admission_client.consume_permission(wire.ConsumePermission(uuid.uuid4().hex, assignment, reference, holder))
        if isinstance(permission, wire.FirstAuthorization):
            break
        if not isinstance(permission.permission, placement.PermissionAvailable):
            print("fixture authorization refused: " + permission.reason, file=sys.stderr, flush=True)
            raise SystemExit(75)
        if time.monotonic() >= permission_deadline:
            print("fixture authorization timed out: " + permission.reason, file=sys.stderr, flush=True)
            raise SystemExit(75)
        time.sleep(0.05)
    with (root / "authorized-executions").open("a") as output:
        output.write(assignment.attempt + "\n")

    fixture_outcome = placement.AttemptOutcomeUnavailable("the execution has not settled")
    fixture_evidence = placement.ExecutionEvidenceUnavailable("the execution has not settled", time.time())
    fixture_cleanup_confirmed = False

    def finish_assigned() -> None:
        settled = fixture_cleanup_confirmed
        if settled:
            release = assigned_mac_test.CliArguments()
            release.pid, release.target = os.getpid(), assignment.destination.target
            release.claim_id, release.episode, release.attempt = identity.claim_id, identity.episode, identity.attempt
            with redirect_stdout(io.StringIO()):
                status = assigned_mac_test.command_release(release, paths)
            settled = status == 0 and not any(claim_state.claim_identity(held.record) == identity
                                             for held in claim_state.read_claims(paths))
        admission_client.report_execution(wire.ExecutionReport(uuid.uuid4().hex, assignment, reference,
            fixture_evidence, fixture_outcome,
            placement.DispatchPathSettled() if settled else placement.DispatchPathPending("the private runner has unresolved work")))

    atexit.register(finish_assigned)

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

''' + ASSIGNED_RUNNER_SETUP + r'''
if "--assignment-file" in arguments:
    fixture_outcome = placement.PrelaunchRefused("runner")
    fixture_evidence = placement.ExecutionNotStarted()
    fixture_cleanup_confirmed = True
if "--copy" in arguments:
    # A Mac copy started by copy_watch.py. With TEST_COPY_CLAIM (mac_test.py)
    # it holds a real copy claim; with TEST_COPY_WAIT=1 it waits for
    # copy-release or SIGTERM; then it writes TEST_COPY_MAC and exits
    # TEST_COPY_STATUS. SIGTERM releases the claim and exits 143.
    import subprocess

    mac_test = "" if "--assignment-file" in arguments else os.environ.get("TEST_COPY_CLAIM", "")
    claimed = False
    claim_identity = ["--claim-id", f"copy-claim-{os.getpid()}", "--episode", f"copy-episode-{os.getpid()}",
                      "--attempt", f"copy-attempt-{os.getpid()}"]

    def release_copy_claim() -> None:
        if claimed:
            subprocess.run([sys.executable, mac_test, "release", "--pid", str(os.getpid()), *claim_identity],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_copy(signal_number: int, _frame: object) -> None:
        global fixture_outcome
        if "--assignment-file" in arguments:
            fixture_outcome = placement.AttemptDeclined("busy")
        release_copy_claim()
        # Renamed into place, so the test never reads it half written.
        (root / "copy-signal.tmp").write_text(str(signal_number), encoding="utf-8")
        os.replace(root / "copy-signal.tmp", root / "copy-signal")
        raise SystemExit(128 + signal_number)

    signal.signal(signal.SIGTERM, stop_copy)
    if mac_test:
        claim = subprocess.run(
            [sys.executable, mac_test, "claim", "--copy", "--pid", str(os.getpid()), "--what", "nextest sample",
             "--worktree", arguments[arguments.index("--repo-root") + 1], *claim_identity],
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
        with open(Path(os.environ["TEST_WAIT_HELPER"]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
        from scripts.direct.wait_for_test_state import wait_for_file
        wait_for_file(root / "copy-release")
    if "--assignment-file" in arguments:
        fixture_outcome = placement.AttemptCompleted(int(os.environ.get("TEST_COPY_STATUS", "1")))
        fixture_evidence = placement.ExecutionEvidenceUnavailable("the private command has ended", time.time())
    result.write_text("mac=" + os.environ.get("TEST_COPY_MAC", "failed") + "\nreason=\nseconds="
                      + os.environ.get("TEST_COPY_SECONDS", "2.5") + "\n", encoding="utf-8")
    release_copy_claim()
    raise SystemExit(int(os.environ.get("TEST_COPY_STATUS", "1")))

event = {
    "args": arguments,
    "token_holder": Path(os.environ["TEST_TOKEN_RECORD"]).read_text().strip() if Path(os.environ["TEST_TOKEN_RECORD"]).exists() else "",
    "coordinator": os.environ.get("BUILDLOG_HOLD_PID", ""),
    "token_held": (Path(os.environ["TEST_TOKEN_RECORD"]).exists()
                   and Path(os.environ["TEST_TOKEN_RECORD"]).read_text().strip() == os.environ.get("BUILDLOG_HOLD_PID", "")),
    "at": time.monotonic(),
    "clock": Path(os.environ["TEST_ROUTING_CLOCK"]).read_text() if os.environ.get("TEST_ROUTING_CLOCK") else "",
}
with (root / "runner-events.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")
(root / "runner-started").write_text("started\n", encoding="utf-8")
forwarded_names = [arguments[index + 1] for index, word in enumerate(arguments) if word == "--env"]
(root / "forwarded-environment.json").write_text(json.dumps({name: os.environ.get(name) for name in forwarded_names}))

attempt = len((root / "runner-events.jsonl").read_text().splitlines())
last_decline = arguments[arguments.index("--last-decline") + 1] if "--last-decline" in arguments else ""

def decline(reason: str, seconds: str) -> None:
    global fixture_outcome, fixture_evidence
    if "--assignment-file" in arguments:
        fixture_outcome = (placement.AttemptDeclined(reason) if reason in ("no_tests", "lost")
                           else placement.PrelaunchRefused(reason))
        fixture_evidence = placement.ExecutionNotStarted()
    refusal = root / "placement-refusal.json"
    ready = refusal.with_suffix(".ready")
    ready.write_text(json.dumps({"reason": reason or "runner", "attempt": attempt,
                                "target": assignment.destination.target if "--assignment-file" in arguments else "mac"}))
    os.replace(ready, refusal)
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
    with open(Path(os.environ["TEST_WAIT_HELPER"]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
    from scripts.direct.wait_for_test_state import wait_for_file
    wait_for_file(root / "continue-retry")

if os.environ.get("TEST_RUNNER_HOLD") == "1":
    def stop(signum: int, _frame: object) -> None:
        global fixture_outcome
        if "--assignment-file" in arguments:
            fixture_outcome = placement.AttemptCancelled("the private runner stopped")
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
if "--assignment-file" in arguments:
    if (mode == "passed" and runner_status == 0) or (mode == "failed" and runner_status not in (0, 75)):
        fixture_outcome = placement.AttemptCompleted(runner_status)
        fixture_evidence = placement.ExecutionEvidenceUnavailable("the private command has ended", time.time())
    elif mode == "lost":
        fixture_outcome = placement.AttemptDeclined("lost")
        fixture_evidence = placement.ExecutionEvidenceUnavailable("the private command has ended", time.time())
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

with open(Path(os.environ["TEST_REAL_TARGETS"]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
from scripts.lib.atomic_file import write_text_atomic

command = sys.argv[1]
root = Path(os.environ["TEST_RUNNER_STATE"])
if command == "wait-local":
    write_text_atomic(root / "local-wait-args", json.dumps(sys.argv[2:]))
if command == "wait-local" and os.environ.get("TEST_LOCAL_WAIT_SECONDS") and not (root / "local-wait-added").exists():
    with Path(os.environ["BUILDLOG_HOLD_WAIT_FILE"]).open("a") as output:
        output.write(os.environ["TEST_LOCAL_WAIT_SECONDS"] + "\n")
    write_text_atomic(root / "local-wait-added", "added\n")
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
if [ "${TEST_PROVIDER_ROUTE_POLL_GATES:-0}" = 1 ]; then
    if [ "$1" != "${BUILD_TARGETS_POLL_S:-15}" ]; then
        exec "$TEST_REAL_SLEEP" "$@"
    fi
    attempts=0
    previous=0
    if [ -s "$root/runner-events.jsonl" ]; then attempts=$(wc -l < "$root/runner-events.jsonl"); fi
    if [ -s "$root/sleep-events" ]; then previous=$(wc -l < "$root/sleep-events"); fi
    if [ "$attempts" -le "$previous" ]; then exit 0; fi
fi
if [ "${TEST_ROUTE_SLEEP_AFTER_RUNNER:-0}" = 1 ] && [ ! -s "$root/runner-events.jsonl" ]; then
    exit 0
fi
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
if [ -n "$gate" ]; then
    exec "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$gate"
fi
'''


# verify invokes this hook through Python; replace its process with the shell
# sleep stand-in so both entry paths share the same event and interval records.
ROUTING_WAIT_STUB = r'''import os
import sys
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
with open(Path(os.environ["TEST_REAL_RECORD"]).resolve().parents[2] / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {"__file__": package_root.name})
runpy.run_path(os.environ["TEST_REAL_RECORD"], run_name="__main__")
'''


ROUTING_CLOCK_BASH = r'''
# Use fixture time only in the disposable verify copy.
# Export the probe for child wait helpers.
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
        self.placement_policy: dict[str, str] = {}
        self.provider_observations: list[placement.HostEvidenceKey] = []

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
        self.provider_observations = []
        if "fixture_seed" in type(self).__dict__:
            self.root = self.fixture_root
            self.addCleanup(shutil.rmtree, self.root)
            _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
            self.fixture_paths()
            self.environment = self.fixture_environment.copy()
        else:
            # Standalone consumers own their fixture through doCleanups().
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
            self.build_fixture()
        self.addCleanup(self.assert_nothing_outlives)
        admission = caller_admission(self, self.root, self.environment)
        admission.configure(waiting_pass_seconds=0.05)

        def provider(key: placement.HostEvidenceKey) -> placement.HostEvidence:
            self.provider_observations.append(key)
            with (self.runner_state / "provider-observations").open("a") as output:
                _ = output.write(key.destination.target + "\n")
            destination = key.destination
            eligibility: placement.HostEligibility = placement.AllowedHost()
            if destination.provider == "local":
                from scripts.build_targets import build_targets

                directory = Path(self.environment["BUILD_HOLD_DIR"])
                blocking = build_targets.hold_waited_on(build_hold.natedev_holds(directory), key.session, directory)
                if not isinstance(blocking, build_targets.NoBlockingHold):
                    eligibility = placement.TemporaryExclusion("blocked")
            else:
                provider_directory = (Path(self.environment["MAC_TEST_STATE_DIR"]) if destination.target == "mac" else
                                      self.home / ".local/state/build-targets" / destination.target)
                available = self.runner_state / "available-gib"
                until = self.placement_policy.get("TEST_RUNNER_BUSY_UNTIL", "")
                allow_runner_refusal = self.placement_policy.get("TEST_PROVIDER_ALLOW_RUNNER_REFUSALS") == "1"
                refusal = self.runner_state / "placement-refusal.json"
                outcome = self.runner_state / f"provider-outcome-{destination.target}.json"
                if outcome.exists():
                    refusal = outcome
                if self.placement_policy.get("TEST_PROVIDER_ROUTE_POLL_GATES") == "1" and refusal.exists():
                    from scripts.build_targets import build_targets

                    recorded = cast(dict[str, object], json.loads(refusal.read_text()))
                    attempt = cast(int, recorded["attempt"])
                    gate = (f"continue-sleep-{attempt}" if self.placement_policy.get("TEST_STEP_ROUTING_SLEEP") == "1"
                            else "continue-sleep")
                    directory = Path(self.environment["BUILD_HOLD_DIR"])
                    holding = build_targets.hold_waited_on(build_hold.natedev_holds(directory), key.session, directory)
                    allow_runner_refusal = ((self.runner_state / gate).exists()
                        and not isinstance(holding, build_targets.NoBlockingHold)
                        and self.placement_policy.get("TEST_RUNNER_BUSY_REASON") != "blocked")
                if self.placement_policy.get("TEST_FREE_NOW_ONCE"):
                    allow_runner_refusal = len(self.runner_events()) < 2
                if refusal.exists() and not allow_runner_refusal:
                    recorded = cast(dict[str, object], json.loads(refusal.read_text()))
                    if (recorded.get("target", "mac") == destination.target
                            and (not until or recorded["reason"] != "busy")):
                        eligibility = placement.TemporaryExclusion(cast(str, recorded["reason"]))
                configured = dict(line.split("=", 1) for line in (self.root / "build_targets.conf").read_text().splitlines()
                                  if "=" in line and not line.lstrip().startswith("#"))
                settings = self.root / configured.get("settings." + destination.target, "mac_test.conf")
                values = dict(line.split("=", 1) for line in settings.read_text().splitlines()
                              if "=" in line and not line.lstrip().startswith("#"))
                if values.get("offload") == "off":
                    eligibility = placement.TemporaryExclusion("off")
                elif ((backoff := provider_directory / "unreachable_until").exists()
                      and float(backoff.read_text()) > time.time()):
                    eligibility = placement.TemporaryExclusion("backoff")
                elif available.exists() and float(available.read_text()) == 0:
                    eligibility = placement.TemporaryExclusion("disk")
                elif (until and not Path(until).exists()
                      and not allow_runner_refusal):
                    reason_file = self.runner_state / "busy-reason"
                    reason = (reason_file.read_text().strip() if reason_file.exists()
                              else self.placement_policy.get("TEST_RUNNER_BUSY_REASON", "busy"))
                    eligibility = placement.TemporaryExclusion(reason)
                else:
                    try:
                        probe = subprocess.run(
                            [str(self.bin_directory / "ssh"), values.get("host", destination.host), "true"],
                            env=self.environment, stdin=subprocess.DEVNULL, capture_output=True,
                            check=False, timeout=1)
                        unreachable = probe.returncode != 0
                    except (OSError, subprocess.TimeoutExpired):
                        unreachable = True
                    if unreachable:
                        eligibility = placement.TemporaryExclusion("unreachable")
            directory = (Path(self.environment["MAC_TEST_STATE_DIR"]) if destination.target == "mac" else
                         self.home / ".local/state/build-targets" / destination.target)
            controller = directory / "controller.json"
            if destination.provider == "ssh" and controller.exists():
                control = cast(dict[str, object], json.loads(controller.read_text()))
                if control.get("state") in ("stopped", "starting", "pending", "stopping"):
                    eligibility = placement.TemporaryExclusion("starting")
            paths = claim_state.StatePaths(directory, destination.target,
                platform="macos" if destination.provider == "mac" else "linux")
            return private_provider_evidence(admission, key, paths, eligibility)

        admission.server.host_probe = provider
        _ = caller_provider_controls(self, admission)

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
        self.write_executable(self.bin_directory / "taplo", "import sys\nif sys.argv[1:] == ['--version']:\n    print('taplo fixture 1.0')\n")
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
        write_pressure_reading(pressure)
        _ = write_config_fixture(self.root / "lint.conf")
        _ = write_config_fixture(self.root / "mac_test.conf")
        settings = self.root / "mac settings.conf"
        _ = write_config_fixture(settings, f"offload=on\nlinux_only.{self.root.name}=\n", template="mac_test")
        config = self.root / "build_targets.conf"
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
            **config_environment(self.root),
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
            "PACKAGE_ROOT": str(self.root),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "VERIFY_STEPS_DIR": str(self.root / "verify-steps"),
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
            "STAFF_FILE": str(self.root / "staff.json"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILD_TARGETS_SCRIPT": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),

            "MAC_TEST_CONFIG": str(settings),
            "CLAUDE_CODE_SESSION_ID": "", "BUILD_TARGETS_NOW": "",
            "BUILD_TARGETS_POLL_S": "0.05",
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader), "CONF_DIR": str(Path(reader).parent),
            "UNIT_RUN_DIR": str(self.board),
            "UNIT_TEAM_ROLE": "test",
            "VERIFY_MAC_RUNNER": str(self.runner),
            "VERIFY_MAC_COPY_WATCH": str(VERIFY.parent.parent / "mac_test" / "copy_watch.py"),
            "VERIFY_MAC_TEST_SCRIPT": str(self.root / "free-now.py"),
            "TEST_RUNNER_STATE": str(self.runner_state),
            "TEST_REAL_PYTHON": sys.executable,
            "TEST_ROUTING_SLEEP_SCRIPT": str(self.bin_directory / "sleep"),
            "TEST_WAIT_HELPER": str(VERIFY.with_name("wait_for_test_state.py")),
            "TEST_CARGO_EVENTS": str(self.cargo_events_path),
            "TEST_TOKEN_RECORD": str(self.board / "locks" / "cargo.d" / "holder_pid"),
            "TEST_METADATA": json.dumps(metadata),
            "TEST_RUNNER_SECONDS": "3.25",
            "TEST_REAL_RECORD": str(VERIFY.parent.parent / "buildlog" / "record.py"),
        }

        self.environment["COPIES_CONFIG"] = copies_environment(self.root)["COPIES_CONFIG"]
        seed_interpreter_cache(self.root / "cache")
        _ = self.environment.pop("CLAUDE_PYTHON", None)

    def write_executable(self, path: Path, source: str) -> None:
        # Private tools may start as links; replacing one must never edit its target.
        if path.is_symlink():
            path.unlink()
        _ = path.write_text(source if source.startswith("#!") else f"#!{sys.executable}\n" + source,
                            encoding="utf-8")
        path.chmod(0o755)

    def assigned_remote(self, target: str, verb: str, kind: str = "test") -> Path:
        """Request a real assignment for a private runner boundary."""
        from scripts.build_queue import admission_client, admission_protocol
        from scripts.mac_test import offload

        path = self.runner_state / f"direct-{target}-{verb}.assignment"
        settings = self.root / ("linux.conf" if target == "peer" else "mac settings.conf")
        with mock.patch.dict(os.environ, self.environment):
            args = offload.parser().parse_args(["run", "--repo-root", str(self.root),
                "--package", "sample", "--settings", str(settings), "--target", target,
                "--verb", verb, "--kind", kind, "--call-id", f"fixture-{os.getpid()}-{target}-{verb}",
                "--result", str(path.with_suffix(".result"))],
                namespace=offload.CliArguments())
            request = offload.submit_direct_placement(offload.request_from_arguments(args), time.time(),
                                                       offload.read_config(settings))
            assert isinstance(request.placement, offload.AssignedOffloadRequest)
            admission_client.write_assignment(path, admission_protocol.Assignment(request.placement.assignment))
        return path

    def call_mac_functions(self, body: str, *, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Call the real result and retry functions with private tools and records."""
        install_fixed_cargo(self.bin_directory / "cargo")
        direct = self.root / "direct-functions"
        direct.mkdir(exist_ok=True)
        library = self.root / "lib"
        if not library.is_symlink():
            library.symlink_to(VERIFY.parent.parent / "lib", target_is_directory=True)
        functions = shell_functions(VERIFY,
            "verify_helper",
            "try_mac_test", "find_mac_runner_pid", "forward_mac_runner_signal", "target_for_settings", "target_platform",
            "release_token", "stop_token_renewer",
            "step_state", "publish_step_state", "tree_key", "set_verdict_keys", "after_tree_change",
            "start_tree_watch", "finish_tree_watch", "stop_tree_watch", "cancel_tree_run",
            "record_mac_pass", "cache_record", "user_clock", "finish_mac_pass", "note_event",
            "collect_hold_wait", "collect_routing_wait", "phase_wait_start", "phase_wait_holder_flags", "phase_wait_finish", "phase_wait_finish_all",
            "finish_hold_wait", "release_wait_claim", "step_can_retry", "remote_step_kind",
            "placement_once", "start_routing_wait", "wait_for_placement", "buildlog_memory_event",
            "pinned_target_refuse", "record_token_wait", "renew_token", "start_token_renewer",
            "try_reserved_target", "mac_only_refuse",
            "route_test", "check_routing_hold")
        functions += shell_functions(VERIFY.parent.parent / "lint/invoke.sh", "buildlog_now")
        functions += shell_functions(VERIFY.parent.parent / "lint/memory_gate.sh",
                                     "build_hold_nothing_held", "build_hold_check_result",
                                     "build_check_stop", "build_check_record_failure")
        script = direct / "verify.sh"
        setup = r'''
set -euo pipefail
PY=$TEST_REAL_PYTHON
CMD=test PKG=sample ARGS=(sample) NEXTEST_ARGS=(-p sample --no-tests=warn)
FILTER_RUN=${TEST_DIRECT_FILTER:-0}
if [[ "$FILTER_RUN" == 1 ]]; then ARGS+=(--filter alpha); NEXTEST_ARGS+=(--filter-expr 'test(alpha)'); fi
GATE_METADATA=$TEST_METADATA
TREE_WATCH_SCRIPT=$TEST_REAL_DIRECT/tree_watch.py
BUILD_FOLDERS_SCRIPT=$TEST_REAL_DIRECT/../lib/build_folders.py
BUILDLOG_RECORD=$HOME/.claude/scripts/buildlog/record.py
BUILDLOG_CALL_ID=direct-$$
export BUILDLOG_CALL_ID BUILDLOG_CALLER=verify
HOLD_WAIT_DIR=$TEST_RUNNER_STATE/hold
mkdir -p "$HOLD_WAIT_DIR"
: > "$HOLD_WAIT_DIR/remote-step-kinds"
BUILDLOG_HOLD_WAIT_FILE=$HOLD_WAIT_DIR/seconds
ROUTING_WAIT_STARTED="" PHASE_WAIT_SESSION="" PHASE_RESOURCE_WAIT=""
PHASE_TOKEN_OBSERVATION="" PHASE_TOKEN_HOLDER=""
PLACEMENT_WAIT_MEMORY=0 PLACEMENT_RETRY_NOTICE_SHOWN=0 STEP_DEFER_LOCAL=0
CACHE_DIR=$UNIT_RUN_DIR/verify_cache
MAC_RUNNER=$VERIFY_MAC_RUNNER MAC_TEST_SCRIPT=$VERIFY_MAC_TEST_SCRIPT
MAC_IDLE_SCRIPT=$TEST_REAL_DIRECT/../buildlog/mac_idle.py
MAC_IDLE_MARKER="" BUILDLOG_MAC_IDLE_HOOK=""
MAC_LEAVE_SETTINGS=$MAC_TEST_CONFIG MAC_LEAVE_REASON=""
MAC_RETRY_AFTER=0 MAC_RETRY_NOTICE_SHOWN=0
SSH_RETIRED_SETTINGS=() SSH_RETRY_SETTINGS=() SSH_RETRY_DEADLINES=()
SSH_RETRY_AFTER_SETTINGS="" SSH_RESERVE_RECOVERY=""
SSH_RETRYABLE_REASON='^(busy|blocked|disk|backoff|unreachable|state|copy|mac_busy)$'
MAC_VERB=nextest MAC_SOURCE_ROOT="" MAC_VERB_ARGS=() MAC_FINISH_CALL=1 MAC_PASSED=0
MAC_ENV_NAMES=() VERIFY_MAC_ENVIRONMENT=() MAC_FETCH_GIVEN=0
MAC_RESULT_FILE="" MAC_LOG_FILE="" MAC_RUNNER_PID="" MAC_ONLY=0 LOCAL_ONLY=0
MAC_RUNNER_LAUNCHING=0 MAC_RUNNER_PRIOR_JOB=""
PINNED_TARGET="" STEP_FORCE_MAC=0
PLACEMENT_STEP_NUMBER=0 PLACEMENT_EPISODE="" PLACEMENT_COMMAND=()
PLACEMENT_ASSIGNMENT_FILE="" PLACEMENT_GRANT_FILE="" PLACEMENT_PROVIDER=""
STEP_RECORD_ACTIVE=0 STEP_WRITES_TREE=0 STEP_RUNS_ON=any_target
STEP_TARGET_TABLE=$(printf 'mac\teligible\t-\t%s\tmacos' "$MAC_TEST_CONFIG")
TREE_WATCH_PID="" TREE_WATCH_DIR="" RUN_STEP_PID="" RUN_LOG=""
VERDICT_KEY="" VERDICT_TREE_KEY="" CALL_NOTED=0
BUILDLOG_MAC="" BUILDLOG_MAC_REASON="" BUILDLOG_MAC_HOLDER="" BUILDLOG_MAC_S=""
BUILDLOG_MAC_COPY="" BUILDLOG_MAC_COPY_S="" BUILDLOG_MAC_COPY_COUNT="" BUILDLOG_MAC_COPY_NATEDEV_S=""
DOC_REPO_TOP=$PACKAGE_ROOT
BOARD_HELPER=$TEST_REAL_DIRECT/board.sh BOARD_DIR=$UNIT_RUN_DIR BOARD_SLOT=direct
TOKEN_HELD=0 TOKEN_RENEWER_PID=""
VERIFY_MONOTONIC_OFFSET_US=0
trap 'finish_tree_watch; finish_hold_wait; release_token' EXIT
'''
        invoke = 'source "$TEST_REAL_DIRECT/../lint/invoke.sh"\n'
        _ = script.write_text(invoke + functions + setup + '\neval "$1"\n', encoding="utf-8")
        process = subprocess.Popen(["bash", str(script), body], cwd=self.root,
                                   env={**self.environment, "TEST_REAL_DIRECT": str(VERIFY.parent),
                                        "TEST_TOKEN_RECORD": str(self.board / "locks/cargo.d/holder_pid"),
                                        **(extra or {})},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True, preexec_fn=restore_sigint)
        return self.finish_verify(process)

    def verify(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.finish_verify(self.start_verify(*arguments, extra=extra))

    def finish_verify(self, process: subprocess.Popen[str]) -> subprocess.CompletedProcess[str]:
        """Stop owned processes before an exceptional exit reaches the next case."""
        try:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            return subprocess.CompletedProcess(process.args, process.wait(timeout=TEST_HANG_GUARD_SECS), output, errors)
        except BaseException:
            try:
                self.capture_queue_state()
            finally:
                self.stop_verify(process)
            raise

    def capture_queue_state(self) -> None:
        """Keep the private queue state before process cleanup changes it."""
        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            state = dict(requests=admission.server.placement_requests,
                         assignments=admission.server.assignments,
                         current=admission.server.current_assignments)
            _ = (self.runner_state / "failed-queue-state.txt").write_text(repr(state))

    def start_verify(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        self.placement_policy = {**self.environment, **(extra or {})}

        install_fixed_cargo(self.bin_directory / "cargo")
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
        for outcome in self.runner_state.glob("provider-outcome-*.json"):
            outcome.unlink()
        for name in ("runner-events.jsonl", "runner-started", "runner-signal", "runner-signals-ready", "placement-refusal.json"):
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
        # Preserve relative helper paths. Replace only clock expressions.
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
        source = source.replace("buildlog_now\n", "BUILDLOG_NOW=$(fixture_routing_clock)\n")
        source = source.replace("${EPOCHREALTIME/./}", "$(fixture_routing_probe)")
        # Fixture time uses monotonic seconds. Its offset is zero.
        source = source.replace(" - VERIFY_MONOTONIC_OFFSET_US", "")
        source = source.replace('"$VERIFY_MONOTONIC_OFFSET_US" "${deadline/./}"', '0 "${deadline/./}"')
        self.verify_script = direct / "verify.sh"
        _ = self.verify_script.write_text(source.replace("set -euo pipefail", ROUTING_CLOCK_BASH + "\nset -euo pipefail", 1))
        self.environment["TEST_ROUTING_CLOCK"] = str(clock)
        return clock

    @contextmanager
    def hold_environment(self, timeout: float = TEST_HANG_GUARD_SECS) -> Generator[None, None, None]:
        with mock.patch.dict(os.environ, {name: self.environment[name] for name in (
            "HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
            "BUILD_HOLD_DIR", "BUILD_HOLD_RELEASE_DIR",
            "BUILDLOG_DIR", "MAC_TEST_STATE_DIR", "NOTIFIER_SESSIONS_DIR")}), \
             mock.patch.dict(os.environ, {"UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
                                          "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": ""}), \
             mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()), \
             hold_recorder_exits(timeout):
            yield

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
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def assert_nothing_outlives(self) -> None:
        # Runs after every other cleanup: a process a call left behind
        # holds no token and no turn, but `build_hold.py quiet` counts it
        # as a cargo run for as long as it lives.
        # Lint recorders intentionally detach from their caller. Wait for their
        # exit events; a two-second grace period races their work under load.
        remaining = wait_for_marker_exit(self.child_marker(), TEST_HANG_GUARD_SECS)
        if remaining:
            survivors = reap(self.child_marker())
            for pid in remaining:
                _ = survivors.setdefault(pid, "exited before reap")
            commands = "\n".join(f"{pid}: {command}" for pid, command in sorted(survivors.items()))
            self.fail(f"processes outlived the test:\n{commands}")

    def start_routing(self, reason: str = "busy", *, pause_retry: bool = False,
                      step_sleep: bool = False, once: Path | None = None) -> subprocess.Popen[str]:
        real_sleep = shutil.which("sleep")
        assert real_sleep is not None
        process = self.start_verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            **({"TEST_FREE_NOW_ONCE": str(once)} if once else {}),
            "TEST_PROVIDER_ROUTE_POLL_GATES": "1", "TEST_REAL_SLEEP": real_sleep,
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": reason, "TEST_PAUSE_ROUTING_SLEEP": "1",
            "TEST_PROVIDER_ALLOW_RUNNER_REFUSALS": "1", "TEST_ROUTE_SLEEP_AFTER_RUNNER": "1",
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
            with local_release_waiter(directory or self.root, self.root / "tmp",
                                      RELEASE_CHANGES | inotify.IN_MODIFY) as events:
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

    def wait_for_memory_wait(self, process: subprocess.Popen[str]) -> None:
        """Wait for a held reply from this fixture's service."""
        from scripts.build_queue import admission_client

        def delivered() -> bool:
            for path in (self.root / "tmp").glob(f"verify-hold.{process.pid}.*/placement-*.json.wait"):
                try:
                    notice = wire.parse_record(path.read_text(), admission_client.PlacementWaitNotice)
                except (OSError, ValueError):
                    continue
                if isinstance(notice.last_reply, wire.Wait):
                    return True
            return False

        self.wait_until(delivered, "the delivered admission wait", (process,))

    def hold_memory(self) -> Path:
        """Hold admission through its private memory reading. Preserve unrelated sleep commands."""
        _ = caller_admission(self, self.root, self.environment).occupy_host()
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n", encoding="utf-8")
        return meminfo

    def leave_once(self) -> dict[str, str]:
        once = self.runner_state / "free-once"
        _ = once.write_text("free\n")
        return {"TEST_FREE_NOW_ONCE": str(once)}

    def free_now_settings(self) -> list[str]:
        settings = self.root / "build_targets.conf"
        values = dict(line.split("=", 1) for line in settings.read_text().splitlines() if "=" in line)
        return [str((settings.parent / values["settings." + key.destination.target]).resolve())
                for key in self.provider_observations if key.destination.provider != "local"]

    def free_now_roots(self) -> list[str]:
        return ["--repo-root " + key.repository for key in self.provider_observations
                if key.destination.provider != "local"]

    def set_mac_answer(self, mode: str, reason: str, status: int) -> None:
        answer = self.runner_state / "runner-answer"
        temporary = answer.with_suffix(".new")
        _ = temporary.write_text(f"{mode}\n{reason}\n{status}\n")
        _ = temporary.replace(answer)

    def runner_episode(self) -> str:
        """Read the episode from the actual runner assignment."""
        from scripts.build_queue import admission_client

        words = self.runner_events()[-1]["args"]
        saved = admission_client.read_assignment(Path(words[words.index("--assignment-file") + 1]))
        return saved.assignment.episode

    def wait_for_retry_probe(self, clock: int, process: subprocess.Popen[str]) -> None:
        """Advance the queue to the requested retry observation."""
        from scripts.build_queue import admission_client

        admission = caller_admission(self, self.root, self.environment)
        controls = caller_provider_controls(self, admission)
        self.wait_until(lambda: any(isinstance(saved.state, placement.ClosedAssignment)
                                  for saved in admission.server.assignments.values()),
                        "a settled remote attempt", (process,))
        def delivered_retry_wait() -> bool:
            for path in (self.root / "tmp").glob(f"verify-hold.{process.pid}.*/placement-*.json.queued"):
                try:
                    queued = wire.parse_record(path.read_text(), admission_client.QueuedPlacementEpisode)
                except (OSError, ValueError):
                    continue
                if queued.episode == self.runner_episode():
                    return True
            return False

        self.wait_until(delivered_retry_wait, "the delivered retry wait", (process,))
        with admission.loop_turn():
            saved = [saved for saved in admission.server.assignments.values()
                     if isinstance(saved.state, placement.ClosedAssignment)][-1]
            assert isinstance(saved.retry, placement.RetryAt)
            event = self.runner_events()[-1]
            assert "clock" in event
            reported_clock = int(event["clock"])
            target = saved.retry.deadline - 180 + clock - reported_clock
            delta = max(0.0, target - admission.clock.now())
        controls.advance(delta)
        with admission.loop_turn():
            _ = admission.server.run_pass()

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
            admission = caller_admission(self, self.root, self.environment)
            previous = admission.server.handle_request
            published = False

            def hold_after_routing(request: wire.Request,
                                   connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
                nonlocal published
                if isinstance(request, wire.ExecutionReport) and not published:
                    _ = shutil.copyfile(record, Path(self.environment["BUILD_HOLD_DIR"]) / "hana")
                    published = True
                return previous(request, connection)

            _ = self.enterContext(mock.patch.object(admission.server, "handle_request", side_effect=hold_after_routing))

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
        admission = caller_admission(self, self.root, self.environment)
        episode = self.runner_episode()
        if wait_kind in ("hold", "first_hold"):
            self.wait_until(lambda: any(candidate.destination.provider == "local"
                    and isinstance(candidate.eligibility, placement.TemporaryExclusion)
                    and candidate.eligibility.reason == "blocked"
                    for candidate in admission.server.placement_eligibility.get(episode, ())),
                "the same episode blocked by the local hold", (process,))
            self.assertIn(episode, admission.server.placement_requests)
            self.assertNotIn(episode, admission.server.current_assignments)

        _ = clock.write_text("279")
        self.wait_for_retry_probe(279, process)
        self.assertEqual(len(self.runner_events()), 1)
        if not repeat_decline:
            self.set_mac_answer("passed", "", 0)
        _ = clock.write_text("280")
        self.wait_for_retry_probe(280, process)
        if repeat_decline:
            self.wait_until(lambda: len(self.runner_events()) == 2, "second Mac attempt", (process,))
            self.wait_until(lambda: sum(isinstance(saved.state, placement.ClosedAssignment)
                    for saved in admission.server.assignments.values()) >= 2,
                "the settled second attempt", (process,))
            _ = clock.write_text("459")
            self.wait_for_retry_probe(459, process)
            self.assertEqual(len(self.runner_events()), 2)
            self.set_mac_answer("passed", "", 0)
            _ = clock.write_text("460")
            self.wait_for_retry_probe(460, process)
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 3 if repeat_decline else 2)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(errors.count("waiting 180 s before each retry"), 1)
        self.assertEqual(self.last_call()["mac"], "passed_full")
        if wait_kind in ("hold", "first_hold"):
            authorized = [saved.assignment.episode for saved in admission.server.assignments.values()
                          if isinstance(saved.permission, placement.PermissionConsumed)]
            self.assertEqual(set(authorized), {episode})
        for event in self.runner_events()[1:]:
            self.assertNotIn("--last-decline", event["args"])

    def start_copy_call(self, *words: str, copy: dict[str, str]) -> tuple[subprocess.Popen[str], int]:
        """Start a call the busy Mac sends to natedev, where its step compiles
        until cargo-release; return it with the pid of the Mac copy it starts."""
        admission = caller_admission(self, self.root, self.environment)
        identity = ["--claim-id", "fixture-busy-claim", "--episode", "fixture-busy-episode",
                    "--attempt", "fixture-busy-attempt"]
        with admission.loop_turn():
            busy = invoke_mac_claim(["claim", "--pid", str(os.getpid()), "--what", "other tests",
                                     "--worktree", str(self.root), *identity], self.environment, self.root)
        self.assertEqual(busy.returncode, 0, busy.stdout + busy.stderr)
        busy_released = False

        def release_busy() -> None:
            nonlocal busy_released
            if busy_released:
                return
            from scripts.mac_test import mac_test

            args = mac_test.CliArguments()
            args.pid, args.target = os.getpid(), "mac"
            args.claim_id, args.episode, args.attempt = "fixture-busy-claim", "fixture-busy-episode", "fixture-busy-attempt"
            with admission.loop_turn():
                status = mac_test.command_release(args, claim_state.StatePaths(Path(self.environment["MAC_TEST_STATE_DIR"])))
            self.assertEqual(status, 0)
            busy_released = True

        self.addCleanup(release_busy)
        previous = admission.server.handle_request

        def release_after_local_grant(request: wire.Request,
                connection: service.Connection | service.NoConnection = service.NO_CONNECTION) -> list[wire.Reply]:
            replies = previous(request, connection)
            if any(isinstance(reply, wire.LocalPlacementGranted) for reply in replies):
                release_busy()
                _ = (self.runner_state / "mac-free").write_text("free\n")
            return replies

        _ = self.enterContext(mock.patch.object(admission.server, "handle_request", side_effect=release_after_local_grant))
        process = self.start_verify("test", "sample", *words, "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_CARGO_HOLD": "1", **copy})
        self.addCleanup(self.stop_verify, process)
        started = self.runner_state / "copy-started"
        self.wait_for_path(self.runner_state / "cargo-compiling", (process,))
        release_busy()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        self.wait_for_path(started, (process,))
        self.assertTrue((self.runner_state / "cargo-compiling").exists())
        self.assertEqual(len(self.runner_events()), 0)
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

