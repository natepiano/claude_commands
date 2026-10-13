#!/usr/bin/env python3
"""Show the recorded state of each configured build target."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import FrameType
from typing import TypedDict, cast


from targets import (
    ANY_ELIGIBLE_TARGET,
    ConfigError,
    EligibleTarget,
    ExpiresAt,
    Free,
    Held,
    LocalTarget,
    LocalRequired,
    ReleaseEstimated,
    Running,
    Routing,
    SshTarget,
    StateUnreadable,
    TargetState,
    Unreachable,
    read_targets,
    target_state,
    target_eligibility,
)
import offload
import build_hold
import mac_test
import override_guard
from user_zone import user_zone



class ActiveWaitProcess(TypedDict):
    call_id: str
    pid: int
    proc_start: str


class CallRegistration(TypedDict):
    key: str
    cycle_id: str


class CliArguments(argparse.Namespace):
    """Arguments accepted by the build-target status command."""

    def __init__(self) -> None:
        super().__init__()
        self.command: str = ""
        self.probe: bool = False
        self.call_id: str = ""
        self.pid: int = 0
        self.signal_name: str = "TERM"
        self.repo_root: str = ""
        self.package: str = ""
        self.local_reason: str = ""


def local_time(instant: datetime) -> str:
    return instant.astimezone(user_zone()).strftime("%H:%M %Z")


def display_field(value: str) -> str:
    return " ".join(value.split())


def format_state(name: str, state: TargetState) -> str:
    name = display_field(name)
    if isinstance(state, Free):
        return f"{name}: free"
    if isinstance(state, Running):
        return f"{name}: running {display_field(state.what)} since {local_time(state.since)}"
    if isinstance(state, Unreachable):
        return f"{name}: unreachable — {display_field(state.reason)}"
    if isinstance(state, StateUnreadable):
        return f"{name}: state unreadable — {display_field(state.reason)}"
    line = f"{name}: held by {display_field(state.holder)} since {local_time(state.since)}"
    if isinstance(state.end, ExpiresAt):
        line += f" until {local_time(state.end.when)}"
    elif isinstance(state.end, ReleaseEstimated):
        line += f" release expected {local_time(state.end.when)}"
    reason = display_field(state.reason)
    return f"{line} — {reason}" if reason else line


def probe_target(target: SshTarget) -> TargetState:
    config = offload.read_config(target.settings)
    result = offload.probe_reachability(offload.ProcessController(), config)
    if isinstance(result, offload.UnreachableHost):
        return Unreachable(result.reason)
    return target_state(target)


def command_status(probe: bool = False) -> int:
    try:
        targets = read_targets()
    except ConfigError as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2
    for target in targets:
        state = (
            probe_target(target)
            if probe and isinstance(target, SshTarget)
            else target_state(target)
        )
        line = format_state(target.name, state)
        if isinstance(target, SshTarget) and isinstance(state, Running):
            try:
                paths = mac_test.state_paths()
                run = mac_test.read_run(paths.run)
                if isinstance(run, dict) and datetime.fromisoformat(run["since"]) == state.since:
                    line += mac_test.claim_idle_clause(run)
            except (OSError, mac_test.StateReadError) as error:
                line = format_state(target.name, StateUnreadable(str(error)))
        print(line)
    return 0


def command_eligible(
    repo_root: Path, package: str, routing: Routing = ANY_ELIGIBLE_TARGET
) -> int:
    try:
        eligibility = target_eligibility(repo_root, package, routing)
    except (ConfigError, OSError, UnicodeError) as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2
    for result in eligibility:
        target = result.target
        disposition = "eligible" if isinstance(result, EligibleTarget) else "excluded"
        reason = "-" if isinstance(result, EligibleTarget) else display_field(result.reason)
        settings = str(target.settings) if isinstance(target, SshTarget) else "-"
        print(f"{target.name}\t{disposition}\t{reason}\t{settings}")
    return 0



def wait_clock() -> datetime:
    supplied = os.environ.get("BUILD_TARGETS_NOW", "")
    return build_hold.aware_instant(supplied) if supplied else datetime.now(timezone.utc)


def wait_sleep(seconds: float) -> None:
    time.sleep(seconds)


def local_target() -> LocalTarget:
    for target in read_targets():
        if target.name == "natedev" and isinstance(target, LocalTarget):
            return target
    raise ConfigError("targets: natedev local target is required")


def wait_diagnostic(reason: str) -> str:
    return f"verify.sh: natedev's hold state cannot be read ({display_field(reason)}); not starting."


def record_admission(admission: build_hold.AdmittedTurn) -> None:
    destination = os.environ.get("BUILDLOG_HOLD_ADMISSION_FILE", "")
    if destination:
        _ = Path(destination).write_text(f"{admission.cycle_id} {admission.generation}\n")


def register_local_call(key: str, call_id: str, pid: int) -> build_hold.Admission:
    """Register the waiting verify process and remember its actual cycle."""
    progress = os.environ.get("BUILDLOG_HOLD_STARTED_FILE", "")
    _ = build_hold.register_wait(key, call_id=call_id, pid=pid,
                                 started=bool(progress and Path(progress).exists()))
    admission = build_hold.read_admission(key, call_id)
    if isinstance(admission, (build_hold.AwaitingTurn, build_hold.AdmittedTurn)):
        destination = os.environ.get("BUILDLOG_HOLD_REGISTRATION_FILE", "")
        if destination:
            registration: CallRegistration = {"key": key, "cycle_id": admission.cycle_id}
            _ = Path(destination).write_text(json.dumps(registration))
    return admission


def command_register_local(call_id: str, *, pid: int) -> int:
    try:
        state = target_state(local_target())
        if isinstance(state, StateUnreadable):
            print(wait_diagnostic(state.reason), file=sys.stderr, flush=True)
            return 12
        if not isinstance(state, Held):
            return 0
        key = build_hold.waiter_key(call_id)
        admission = build_hold.read_admission(key, call_id)
        if isinstance(admission, build_hold.AwaitingTurn):
            admission = register_local_call(key, call_id, pid)
        if isinstance(admission, build_hold.AdmissionReadError):
            print(wait_diagnostic(admission.reason), file=sys.stderr, flush=True)
            return 12
        if isinstance(admission, build_hold.AdmittedTurn):
            record_admission(admission)
            return 0
        print(f"verify.sh: natedev is held by {display_field(state.holder)} for "
              + f"{display_field(state.reason)}.",
              file=sys.stderr, flush=True)
        escalation = float(os.environ.get("BUILD_TARGETS_ESCALATE_S", "7200"))
        if (wait_clock() - state.since).total_seconds() >= escalation:
            print(f"verify.sh: natedev has been held for 2 h by {display_field(state.holder)}; ask them.",
                  file=sys.stderr, flush=True)
        return 10
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error)), file=sys.stderr, flush=True)
        return 12


def command_wait_local(call_id: str, *, pid: int = 0) -> int:
    key = ""
    started = wait_clock()
    announced = False
    escalated = False
    registered: tuple[str, int] = ("", 0)
    process = pid or os.getpid()
    cancelled = False
    waiter_file = os.environ.get("BUILDLOG_HOLD_WAITER_FILE", "")
    previous_handlers: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}

    def interrupted(signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = True
        raise SystemExit(128 + signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        if waiter_file:
            waiter: ActiveWaitProcess = {"call_id": call_id, "pid": os.getpid(),
                                         "proc_start": build_hold.process_start_time(os.getpid())}
            temporary = Path(waiter_file).with_suffix(".tmp")
            _ = temporary.write_text(json.dumps(waiter))
            os.replace(temporary, waiter_file)
        target = local_target()
        interval = max(0.001, float(os.environ.get("BUILD_TARGETS_POLL_S", "15")))
        escalation = float(os.environ.get("BUILD_TARGETS_ESCALATE_S", "7200"))
        while True:
            state = target_state(target)
            if isinstance(state, StateUnreadable):
                print(wait_diagnostic(state.reason), file=sys.stderr, flush=True)
                return 12
            if not isinstance(state, Held):
                return 0
            if not key:
                key = build_hold.waiter_key(call_id)
            admission = build_hold.read_admission(key, call_id)
            if isinstance(admission, build_hold.AdmissionReadError):
                print(wait_diagnostic(admission.reason), file=sys.stderr, flush=True)
                return 12
            if isinstance(admission, build_hold.AdmittedTurn):
                record_admission(admission)
                return 0
            if isinstance(admission, build_hold.AwaitingTurn):
                identity = (admission.cycle_id, admission.generation)
                if identity != registered:
                    updated = register_local_call(key, call_id, process)
                    if isinstance(updated, (build_hold.AwaitingTurn, build_hold.AdmittedTurn)):
                        registered = (updated.cycle_id, updated.generation)
                    else:
                        registered = identity
                    continue
            if not announced:
                print(f"verify.sh: natedev is held by {display_field(state.holder)} for "
                      + f"{display_field(state.reason)}; waiting (tests that can run on the Mac keep running there).",
                      file=sys.stderr, flush=True)
                announced = True
            if not escalated and (wait_clock() - state.since).total_seconds() >= escalation:
                print(f"verify.sh: natedev has been held for 2 h by {display_field(state.holder)}; ask them.",
                      file=sys.stderr, flush=True)
                escalated = True
            wait_sleep(interval)
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error)), file=sys.stderr, flush=True)
        return 12
    finally:
        if cancelled and key:
            try:
                build_hold.remove_call(key, call_id)
            except (OSError, build_hold.ReleaseRecordReadError):
                pass
        for signum, handler in previous_handlers.items():
            _ = signal.signal(signum, handler)
        output = os.environ.get("BUILDLOG_HOLD_WAIT_FILE", "")
        if output:
            with Path(output).open("a") as elapsed:
                seconds = max(0.0, (wait_clock() - started).total_seconds()) if announced else 0.0
                _ = elapsed.write(f"{seconds:.6f}\n")
        if waiter_file:
            Path(waiter_file).unlink(missing_ok=True)


def command_interrupt_local(call_id: str, signal_name: str) -> int:
    waiter_file = os.environ.get("BUILDLOG_HOLD_WAITER_FILE", "")
    if not waiter_file:
        return 0
    path = Path(waiter_file)
    try:
        record = cast(object, json.loads(path.read_text()))
        if not isinstance(record, dict):
            return 12
        waiter = cast(dict[str, object], record)
        pid = waiter.get("pid")
        identity = waiter.get("proc_start")
        if (waiter.get("call_id") != call_id or not isinstance(pid, int) or pid <= 0
            or not isinstance(identity, str) or not identity
            or build_hold.process_start_time(pid) != identity):
            return 0
        os.kill(pid, signal.SIGINT if signal_name == "INT" else signal.SIGTERM)
        deadline = time.monotonic() + 2
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return 0
    except (FileNotFoundError, ProcessLookupError):
        return 0
    except (OSError, ValueError):
        return 12


def command_check_admission(call_id: str) -> int:
    try:
        state = target_state(local_target())
        if isinstance(state, StateUnreadable):
            print(wait_diagnostic(state.reason), file=sys.stderr)
            return 12
        if not isinstance(state, Held):
            return 0
        admission = build_hold.read_admission(build_hold.waiter_key(call_id), call_id)
        if isinstance(admission, build_hold.AdmissionReadError):
            print(wait_diagnostic(admission.reason), file=sys.stderr)
            return 12
        if isinstance(admission, build_hold.AdmittedTurn):
            record_admission(admission)
            return 0
        return 10
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error)), file=sys.stderr)
        return 12


def holds_only_own(owner: build_hold.OwningSession, directory: Path) -> bool:
    try:
        holds = build_hold.natedev_holds(directory)
        key = build_hold.holder_path(directory, owner.session_id).name
    except ValueError:
        return False
    return bool(holds.in_force) and all(holder.key == key for holder in holds.in_force)


def command_check_local() -> int:
    try:
        state = target_state(local_target())
        if isinstance(state, StateUnreadable):
            print(wait_diagnostic(state.reason))
            return 12
        if not isinstance(state, Held):
            return 0
        owner = build_hold.owning_session()
        if isinstance(owner, build_hold.OwningSession) and holds_only_own(owner, build_hold.holder_directory()):
            return 0
        key = owner.session_id if isinstance(owner, build_hold.OwningSession) else ""
        admission = build_hold.read_admission(key)
        if isinstance(admission, build_hold.AdmissionReadError):
            print(wait_diagnostic(admission.reason))
            return 12
        if isinstance(admission, build_hold.AdmittedTurn):
            return 0
        if key:
            _ = build_hold.register_wait(key)
        print(f"natedev is held by {display_field(state.holder)} for {display_field(state.reason)}; launch after the release.")
        return 10
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error)))
        return 12


def command_finish_local(call_id: str) -> int:
    destination = os.environ.get("BUILDLOG_HOLD_REGISTRATION_FILE", "")
    if not destination:
        return 0
    try:
        record = cast(object, json.loads(Path(destination).read_text()))
        if not isinstance(record, dict):
            return 12
        registration = cast(dict[str, object], record)
        key = registration.get("key")
        cycle_id = registration.get("cycle_id")
        if not isinstance(key, str) or not isinstance(cycle_id, str):
            return 12
        build_hold.remove_call(key, call_id, cycle_id=cycle_id)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return 12
    return 0


def positive_pid(value: str) -> int:
    pid = int(value)
    if pid <= 0:
        raise argparse.ArgumentTypeError("must be a positive PID")
    return pid


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status")
    _ = status.add_argument("--probe", action="store_true")
    eligible = commands.add_parser("eligible")
    _ = eligible.add_argument("--repo-root", required=True)
    _ = eligible.add_argument("--package", required=True)
    _ = eligible.add_argument("--local", dest="local_reason", default="")
    register = commands.add_parser("register-local")
    _ = register.add_argument("--call-id", required=True)
    _ = register.add_argument("--pid", type=positive_pid, required=True)
    waiter = commands.add_parser("wait-local")
    _ = waiter.add_argument("--call-id", required=True)
    _ = waiter.add_argument("--pid", type=positive_pid, default=0)
    _ = commands.add_parser("check-local")
    admission = commands.add_parser("check-admission")
    _ = admission.add_argument("--call-id", required=True)
    interrupt = commands.add_parser("interrupt-local")
    _ = interrupt.add_argument("--call-id", required=True)
    _ = interrupt.add_argument("--signal", dest="signal_name", choices=("INT", "TERM"), default="TERM")
    finish = commands.add_parser("finish-local")
    _ = finish.add_argument("--call-id", required=True)
    return root


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "build_targets/build_targets.py")
    args = parser().parse_args(namespace=CliArguments())
    if args.command == "eligible":
        routing = LocalRequired(args.local_reason) if args.local_reason else ANY_ELIGIBLE_TARGET
        return command_eligible(Path(args.repo_root).resolve(), args.package, routing)
    if args.command == "register-local":
        return command_register_local(args.call_id, pid=args.pid)
    if args.command == "wait-local":
        return command_wait_local(args.call_id, pid=args.pid)
    if args.command == "check-local":
        return command_check_local()
    if args.command == "check-admission":
        return command_check_admission(args.call_id)
    if args.command == "interrupt-local":
        return command_interrupt_local(args.call_id, args.signal_name)
    if args.command == "finish-local":
        return command_finish_local(args.call_id)
    return command_status(args.probe)


if __name__ == "__main__":
    raise SystemExit(main())
