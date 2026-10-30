#!/usr/bin/env python3
"""Show the recorded state of each configured build target."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast, final

if TYPE_CHECKING:
    from ..lib import process_start
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from targets import (
    ANY_ELIGIBLE_TARGET,
    ConfigError,
    EligibleTarget,
    ExcludedTarget,
    ExpiresAt,
    Free,
    Held,
    HolderSession,
    IdleSince,
    LocalTarget,
    LocalRequired,
    ReleaseEstimated,
    Running,
    RunningJob,
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
import override_guard
from user_zone import user_zone
if TYPE_CHECKING:
    from ..message import roster
else:
    from scripts.message import roster



class ActiveWaitProcess(TypedDict):
    call_id: str
    pid: int
    proc_start: str


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
        self.kind: str = ""
        self.local_reason: str = ""
        self.leave_hook: str = ""
        self.caller: str = ""


def local_time(instant: datetime) -> str:
    return instant.astimezone(user_zone()).strftime("%H:%M %Z")


def display_field(value: str) -> str:
    return " ".join(value.split())


def unit_label_suffix(state: RunningJob | Held, labels: Mapping[RunningJob | Held, roster.UnitLabels]) -> str:
    identity = labels.get(state)
    if identity is None:
        return ""
    return f" (showrunner {display_field(identity.showrunner)}, unit {display_field(identity.unit)})"


_NO_UNIT_LABELS: Mapping[RunningJob | Held, roster.UnitLabels] = {}


def running_job_text(job: RunningJob,
                     labels: Mapping[RunningJob | Held, roster.UnitLabels] = _NO_UNIT_LABELS) -> str:
    text = f"running {display_field(job.what)} since {local_time(job.since)}"
    text += unit_label_suffix(job, labels)
    if isinstance(job.idle, IdleSince):
        text += f", idle since {job.idle.when.astimezone(user_zone()).strftime('%H:%M')}"
    return text


def format_state(name: str, state: TargetState,
                 labels: Mapping[RunningJob | Held, roster.UnitLabels] = _NO_UNIT_LABELS) -> str:
    name = display_field(name)
    if isinstance(state, Free):
        return f"{name}: free"
    if isinstance(state, Running):
        return f"{name}: " + "; ".join(running_job_text(job, labels) for job in state.jobs)
    if isinstance(state, Unreachable):
        return f"{name}: unreachable — {display_field(state.reason)}"
    if isinstance(state, StateUnreadable):
        return f"{name}: state unreadable — {display_field(state.reason)}"
    line = f"{name}: held by {display_field(state.holder)} since {local_time(state.since)}"
    line += unit_label_suffix(state, labels)
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
    states: list[tuple[str, TargetState]] = []
    holders: dict[RunningJob | Held, str] = {}
    for target in targets:
        state = (
            probe_target(target)
            if probe and isinstance(target, SshTarget)
            else target_state(target)
        )
        states.append((target.name, state))
        jobs = state.jobs if isinstance(state, Running) else (state,) if isinstance(state, Held) else ()
        for job in jobs:
            if isinstance(job.holder_session, HolderSession):
                holders[job] = job.holder_session.session_id
    try:
        identities = roster.unit_labels_for(holders.values(), os.environ) if holders else {}
    except Exception:
        identities = {}
    labels = {state: identities[session_id] for state, session_id in holders.items() if session_id in identities}
    for name, state in states:
        print(format_state(name, state, labels))
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
        print_eligible_target(result)
    return 0


def print_eligible_target(result: EligibleTarget | ExcludedTarget) -> None:
    target = result.target
    disposition = "eligible" if isinstance(result, EligibleTarget) else "excluded"
    reason = "-" if isinstance(result, EligibleTarget) else display_field(result.reason)
    settings = str(target.settings) if isinstance(target, SshTarget) else "-"
    print(f"{target.name}\t{disposition}\t{reason}\t{settings}")


StepKind = Literal["check", "lint", "doc", "test", "build", "example", "final"]


@dataclass(frozen=True)
class MacAllowed:
    pass


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


def mac_target_eligibility(target: LocalTarget | SshTarget, repo_root: Path,
                           package: str, kind: StepKind, routing: Routing
                           ) -> EligibleTarget | ExcludedTarget:
    if kind in ("build", "example"):
        return ExcludedTarget(target, "makes natedev artifacts")
    if kind == "final":
        return ExcludedTarget(target, "full gate runs on natedev")
    if isinstance(routing, LocalRequired):
        return ExcludedTarget(target, routing.reason)
    verb: offload.MacVerb = "nextest" if kind == "test" else "clippy" if kind == "lint" else kind
    try:
        config = offload.read_config(target.settings) if isinstance(target, SshTarget) else offload.read_config()
        eligibility = offload.mac_step_eligibility(config, repo_root, package, verb)
        if isinstance(eligibility, offload.IneligibleMacStep):
            return ExcludedTarget(target, eligibility.reason)
        probe = offload.probe_mac(offload.ProcessController(), config, eligibility)
        if isinstance(probe, offload.UnavailableProbe):
            return ExcludedTarget(target, "unreachable")
        if probe.free_gib < config.free_floor_gib:
            return ExcludedTarget(target, "disk")
    except (OSError, UnicodeError, ValueError) as error:
        return ExcludedTarget(target, f"state unreadable: {error}")
    return EligibleTarget(target)


def command_kind_eligible(repo_root: Path, package: str, kind: StepKind, routing: Routing) -> int:
    try:
        eligibility = tuple(
            mac_target_eligibility(target, repo_root, package, kind, routing)
            if target.platform == "macos" else EligibleTarget(target)
            for target in read_targets()
        )
    except (ConfigError, OSError, UnicodeError, ValueError) as error:
        print(f"natedev_only state unreadable: {display_field(str(error))}")
        return 0
    decision: MacAllowed | NatedevOnly = NatedevOnly("no Mac target configured")
    if kind in ("build", "example"):
        decision = NatedevOnly("makes natedev artifacts")
    elif kind == "final":
        decision = NatedevOnly("full gate runs on natedev")
    elif isinstance(routing, LocalRequired):
        decision = NatedevOnly(routing.reason)
    for result in eligibility:
        if result.target.platform != "macos":
            continue
        if isinstance(result, EligibleTarget):
            decision = MacAllowed()
            break
        decision = NatedevOnly(result.reason)
    print("mac_allowed" if isinstance(decision, MacAllowed) else f"natedev_only {display_field(decision.reason)}")
    for result in eligibility:
        print_eligible_target(result)
    return 0


def wait_clock() -> datetime:
    supplied = os.environ.get("BUILD_TARGETS_NOW", "")
    return build_hold.aware_instant(supplied) if supplied else datetime.now(timezone.utc)


@final
class NoActiveLocalHoldWait:
    """No wait owns a release-wait mechanism in this execution context."""

    def wait(self, _seconds: float) -> None:
        raise RuntimeError("local hold waiting requires an active wait")


class LocalHoldWait(Protocol):
    """A hold wait owns and closes its notification or polling mechanism."""

    def arm(self) -> None: ...
    def wait(self, seconds: float) -> None: ...
    def __enter__(self) -> "LocalHoldWait": ...
    def __exit__(self, *_arguments: object) -> None: ...


# This CLI's import root excludes worker; keep the runtime import at a typed
# provider boundary rather than expanding the shared checker configuration.
release_waiter = cast(Callable[[Path, Path], LocalHoldWait], getattr(
    importlib.import_module("scripts.direct.local_release_wait"), "local_release_waiter"))

LOCAL_HOLD_WAIT: ContextVar[LocalHoldWait | NoActiveLocalHoldWait] = ContextVar(
    "local_hold_wait", default=NoActiveLocalHoldWait())


def wait_sleep(seconds: float) -> None:
    # Keep the existing wait boundary; its owner selects notifications or polling.
    LOCAL_HOLD_WAIT.get().wait(seconds)


def local_target() -> LocalTarget:
    for target in read_targets():
        if target.name == "natedev" and isinstance(target, LocalTarget):
            return target
    raise ConfigError("targets: natedev local target is required")


# wait-local's status when its leave hook found another target free: the call
# leaves natedev's queue and runs there. It is invoke.sh's LINT_LEFT_QUEUE_STATUS.
LEFT_QUEUE_STATUS = 75


def leave_hook_says_free(leave_hook: str) -> bool:
    """Run the caller's leave hook (mac_test.py free-now); status 0 means free."""
    if not leave_hook:
        return False
    try:
        return subprocess.run(["/bin/sh", "-c", leave_hook], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False).returncode == 0
    except OSError:
        return False


VERIFY_SPEAKER = "verify.sh"


def speaker(caller: str) -> str:
    """The name the wait messages start with: verify.sh, or lint and the caller that ran it."""
    if not caller:
        return VERIFY_SPEAKER
    return "lint" if caller == "lint" else f"lint ({display_field(caller)})"


def wait_diagnostic(reason: str, caller: str = "") -> str:
    return f"{speaker(caller)}: natedev's hold state cannot be read ({display_field(reason)}); not starting."


def holds_only_own(owner: build_hold.OwningSession, directory: Path) -> bool:
    try:
        holds = build_hold.natedev_holds(directory)
        key = build_hold.holder_path(directory, owner.session_id).name
    except ValueError:
        return False
    return bool(holds.in_force) and all(holder.key == key for holder in holds.in_force)


def caller_holds_only_own() -> bool:
    """The calling session's hold is the only one in force, so natedev is held for it, not against it."""
    owner = build_hold.owning_session()
    return isinstance(owner, build_hold.OwningSession) and holds_only_own(owner, build_hold.holder_directory())


def command_register_local(_call_id: str, *, pid: int) -> int:
    """Check the local hold without waiting or writing a call record."""
    _ = pid
    return command_check_local("verify")


def command_wait_local(call_id: str, *, pid: int = 0, leave_hook: str = "", caller: str = "") -> int:
    """Wait until no hold applies. An empty caller is verify.sh; any other caller is a lint step."""
    _ = pid
    if not leave_hook and not caller:
        leave_hook = os.environ.get("BUILDLOG_MAC_IDLE_HOOK", "")
    name = speaker(caller)
    # verify.sh runs a test on the Mac when it can; a lint step only waits.
    waiting = "; waiting (tests that can run on the Mac keep running there)." if not caller else "; waiting for the release."
    started = wait_clock()
    announced = False
    escalated = False
    polled = False
    waiter_file = os.environ.get("BUILDLOG_HOLD_WAITER_FILE", "")
    previous_handlers: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}
    notifications = ExitStack()

    def interrupted(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupted)
        if waiter_file:
            owner_start = process_start.process_start(os.getpid())
            if isinstance(owner_start, process_start.ProcessGone):
                raise RuntimeError("hold waiter process ended before registration")
            waiter: ActiveWaitProcess = {"call_id": call_id, "pid": os.getpid(),
                                         "proc_start": owner_start.text}
            temporary = Path(waiter_file).with_suffix(".tmp")
            _ = temporary.write_text(json.dumps(waiter))
            os.replace(temporary, waiter_file)
        target = local_target()
        release_events = notifications.enter_context(release_waiter(
            build_hold.holder_directory(), build_hold.release_directory()))
        context = LOCAL_HOLD_WAIT.set(release_events)
        _ = notifications.callback(LOCAL_HOLD_WAIT.reset, context)
        interval = max(0.001, float(os.environ.get("BUILD_TARGETS_POLL_S", "15")))
        escalation = float(os.environ.get("BUILD_TARGETS_ESCALATE_S", "7200"))
        while True:
            release_events.arm()
            state = target_state(target)
            if isinstance(state, StateUnreadable):
                print(wait_diagnostic(state.reason, caller), file=sys.stderr, flush=True)
                return 12
            holds = build_hold.natedev_holds(build_hold.holder_directory()).in_force
            if not holds or caller_holds_only_own():
                return 0
            if not isinstance(state, Held):
                print(wait_diagnostic("holder file exists; target state disagrees", caller), file=sys.stderr, flush=True)
                return 12
            if not announced:
                announced = True
                print(f"{name}: natedev is held by {display_field(state.holder)} for "
                      + f"{display_field(state.reason)}{waiting}",
                      file=sys.stderr, flush=True)
            # A held natedev cannot start this step, so a free Mac wins and the
            # call can leave without an allocation. As in memory_gate.sh, the
            # hook runs from the second poll on: a Mac that declines after
            # free-now said free costs one poll interval, never a tight loop.
            if polled and leave_hook_says_free(leave_hook):
                print(f"{name}: the Mac is free: leaving natedev's hold wait to run there.",
                      file=sys.stderr, flush=True)
                return LEFT_QUEUE_STATUS
            if not escalated and (wait_clock() - state.since).total_seconds() >= escalation:
                print(f"{name}: natedev has been held for 2 h by {display_field(state.holder)}; ask them.",
                      file=sys.stderr, flush=True)
                escalated = True
            wait_sleep(interval)
            polled = True
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error), caller), file=sys.stderr, flush=True)
        return 12
    finally:
        notifications.close()
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
            or process_start.check_owner(pid, process_start.parse_start(identity)) is not process_start.OwnerCheck.ALIVE):
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


def command_check_local(caller: str = "") -> int:
    try:
        state = target_state(local_target())
        output = sys.stderr if caller else sys.stdout
        if isinstance(state, StateUnreadable):
            print(wait_diagnostic(state.reason, "" if caller == "verify" else caller), file=output)
            return 12
        holds = build_hold.natedev_holds(build_hold.holder_directory()).in_force
        if not holds or caller_holds_only_own():
            return 0
        if not isinstance(state, Held):
            print(wait_diagnostic("holder file exists; target state disagrees", caller), file=output)
            return 12
        if caller:
            label = speaker("" if caller == "verify" else caller)
            print(f"{label}: natedev is held by {display_field(state.holder)} for {display_field(state.reason)}.", file=output)
            escalation = float(os.environ.get("BUILD_TARGETS_ESCALATE_S", "7200"))
            if (wait_clock() - state.since).total_seconds() >= escalation:
                print(f"{label}: natedev has been held for 2 h by {display_field(state.holder)}; ask them.", file=output)
        else:
            owner = build_hold.owning_session()
            if isinstance(owner, build_hold.OwningSession):
                _ = build_hold.register_wait(owner.session_id)
            print(f"natedev is held by {display_field(state.holder)} for {display_field(state.reason)}; launch after the release.")
        return 10
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error), "" if caller == "verify" else caller), file=sys.stderr if caller else sys.stdout)
        return 12


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
    _ = eligible.add_argument("--package", default="")
    _ = eligible.add_argument("--kind", choices=("check", "lint", "doc", "test", "build", "example", "final"), default="")
    _ = eligible.add_argument("--local", dest="local_reason", default="")
    register = commands.add_parser("register-local")
    _ = register.add_argument("--call-id", required=True)
    _ = register.add_argument("--pid", type=positive_pid, required=True)
    waiter = commands.add_parser("wait-local")
    _ = waiter.add_argument("--call-id", required=True)
    _ = waiter.add_argument("--pid", type=positive_pid, default=0)
    _ = waiter.add_argument("--leave-hook", default="")
    _ = waiter.add_argument("--caller", default="")
    check = commands.add_parser("check-local")
    _ = check.add_argument("--caller", default="")
    interrupt = commands.add_parser("interrupt-local")
    _ = interrupt.add_argument("--call-id", required=True)
    _ = interrupt.add_argument("--signal", dest="signal_name", choices=("INT", "TERM"), default="TERM")
    return root


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "build_targets/build_targets.py")
    args = parser().parse_args(namespace=CliArguments())
    if args.command == "eligible":
        routing = LocalRequired(args.local_reason) if args.local_reason else ANY_ELIGIBLE_TARGET
        if args.kind:
            return command_kind_eligible(Path(args.repo_root).resolve(), args.package, cast(StepKind, args.kind), routing)
        return command_eligible(Path(args.repo_root).resolve(), args.package, routing)
    if args.command == "register-local":
        return command_register_local(args.call_id, pid=args.pid)
    if args.command == "wait-local":
        return command_wait_local(args.call_id, pid=args.pid, leave_hook=args.leave_hook, caller=args.caller)
    if args.command == "check-local":
        return command_check_local(args.caller)
    if args.command == "interrupt-local":
        return command_interrupt_local(args.call_id, args.signal_name)
    return command_status(args.probe)


if __name__ == "__main__":
    raise SystemExit(main())
