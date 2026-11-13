#!/usr/bin/env python3
"""Show the recorded state of each configured build target."""

from __future__ import annotations

from typing import TYPE_CHECKING
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import importlib
import io
import json
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack, nullcontext, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Literal, Protocol, TypedDict, cast, final

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_hold import override_guard
from scripts.mac_test import claim_state
from scripts.build_queue.placement import ConfiguredProviderSettings, ExplicitProviderSettings, ProviderSettings

CONFIGURED_PROVIDER_SETTINGS = ConfiguredProviderSettings()


if TYPE_CHECKING:
    from scripts.build_hold import build_hold
    from scripts.build_queue.placement import SubmittingSession
    from scripts.build_queue import placement
    from scripts.mac_test import mac_test, offload
    from scripts.message import roster
    from scripts.build_targets.targets import (
        AwsStatusRow, Held, RunningJob, TargetState,
    )


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
        self.reservation_file: str = ""
        self.target: str = ""
        self.new_step: bool = False
        self.recovery_fd: int = -1
        self.probe_marks_file: str = ""
        self.recover_only: bool = False
        self.mac_only: bool = False
        self.remote_kinds_file: str = ""
        self.session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION
        self.claim_identity_file: str = ""
        self.claim_id: str = ""
        self.episode: str = ""
        self.attempt: str = ""
        self.assignment_file: str = ""


def positive_pid(value: str) -> int:
    pid = int(value)
    if pid <= 0:
        raise argparse.ArgumentTypeError("must be a positive PID")
    return pid


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    reserve = commands.add_parser("reserve", aliases=["recover-reservation"])
    _ = reserve.add_argument("--repo-root", required=True)
    _ = reserve.add_argument("--package", default="")
    _ = reserve.add_argument("--kind", required=True)
    _ = reserve.add_argument("--pid", type=positive_pid, required=True)
    _ = reserve.add_argument("--reservation-file", required=True)
    _ = reserve.add_argument("--claim-identity-file", default="")
    _ = reserve.add_argument("--assignment-file", default="")
    _ = reserve.add_argument("--target", default="")
    _ = reserve.add_argument("--recover-only", action="store_true")
    _ = reserve.add_argument("--new-step", action="store_true")
    info = commands.add_parser("assigned-info")
    _ = info.add_argument("--assignment-file", required=True)
    recovery = commands.add_parser("recover-probe", help=argparse.SUPPRESS)
    _ = recovery.add_argument("--repo-root", required=True)
    _ = recovery.add_argument("--package", default="")
    _ = recovery.add_argument("--kind", required=True)
    _ = recovery.add_argument("--target", required=True)
    _ = recovery.add_argument("--recovery-fd", type=int, required=True)
    release = commands.add_parser("release-wait")
    _ = release.add_argument("--pid", type=positive_pid, required=True)
    for field in ("claim-id", "episode", "attempt"):
        _ = release.add_argument("--" + field, default="")

    def parsed_session(value: str) -> SubmittingSession:
        return OwningSession(value) if value else NoOwningSession()

    _ = release.add_argument("--session", type=parsed_session, default=claim_state.CALLING_SESSION)
    _ = commands.add_parser("submitting-session")
    source = commands.add_parser("source-key")
    _ = source.add_argument("--repo-root", required=True)
    status = commands.add_parser("status")
    _ = status.add_argument("--probe", action="store_true")
    eligible = commands.add_parser("eligible", aliases=["constraints"])
    _ = eligible.add_argument("--repo-root", required=True)
    _ = eligible.add_argument("--package", default="")
    _ = eligible.add_argument("--kind", choices=("check", "lint", "doc", "test", "build", "example", "final"), default="")
    _ = eligible.add_argument("--local", dest="local_reason", default="")
    _ = eligible.add_argument("--target", default="")
    _ = eligible.add_argument("--mac", dest="mac_only", action="store_true")
    _ = eligible.add_argument("--remote-kinds-file", default="")
    _ = eligible.add_argument("--new-step", action="store_true")
    _ = eligible.add_argument("--probe-marks-file", default="")
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
    for local_command in (register, waiter, check):
        _ = local_command.add_argument("--session", type=parsed_session, default=claim_state.CALLING_SESSION)
    interrupt = commands.add_parser("interrupt-local")
    _ = interrupt.add_argument("--call-id", required=True)
    _ = interrupt.add_argument("--signal", dest="signal_name", choices=("INT", "TERM"), default="TERM")
    return root


from scripts.build_targets.target_config import (
    ANY_ELIGIBLE_TARGET,
    ConfigError,
    EligibleTarget,
    ExcludedTarget,
    LocalTarget,
    LocalRequired,
    Routing,
    SshTarget,
    read_targets,
)
from scripts.direct.step_selection import Pinned, ROUTED, TargetSelection


class ActiveWaitProcess(TypedDict):
    call_id: str
    pid: int
    proc_start: str


def target_state(target: LocalTarget | SshTarget) -> TargetState:
    """Load the state reader only when the command reads target state."""
    from scripts.build_targets.targets import target_state as read_target_state

    return read_target_state(target)


def local_time(instant: datetime) -> str:
    from scripts.lib.user_zone import user_zone

    return instant.astimezone(user_zone()).strftime("%H:%M %Z")


def display_field(value: str) -> str:
    from scripts.buildlog.mac_idle_alert import display_field as clean_field

    return clean_field(value)


def unit_label_suffix(state: RunningJob | Held, labels: Mapping[RunningJob | Held, roster.UnitLabels]) -> str:
    identity = labels.get(state)
    if identity is None:
        return ""
    return f" (showrunner {display_field(identity.showrunner)}, unit {display_field(identity.unit)})"


_NO_UNIT_LABELS: Mapping[RunningJob | Held, roster.UnitLabels] = {}


def running_job_text(job: RunningJob,
                     labels: Mapping[RunningJob | Held, roster.UnitLabels] = _NO_UNIT_LABELS) -> str:
    from scripts.lib.user_zone import user_zone
    from scripts.build_targets.targets import IdleSince

    text = f"running {display_field(job.what)} since {local_time(job.since)}"
    text += unit_label_suffix(job, labels)
    if isinstance(job.idle, IdleSince):
        text += f", idle since {job.idle.when.astimezone(user_zone()).strftime('%H:%M')}"
    return text


def format_state(name: str, state: TargetState,
                 labels: Mapping[RunningJob | Held, roster.UnitLabels] = _NO_UNIT_LABELS) -> str:
    from scripts.build_targets.targets import (
        AwsStatusRow, ExpiresAt, Free, ReleaseEstimated, Running, StateUnreadable, Unreachable,
    )

    name = display_field(name)
    if isinstance(state, AwsStatusRow):
        return format_aws_status(state)
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
    from scripts.build_targets import target_config
    from scripts.build_targets.targets import Unreachable
    from scripts.mac_test import offload

    if target_config.is_aws_target(target):
        return target_state(target)
    config = offload.read_config(target.settings)
    result = offload.probe_reachability(offload.ProcessController(), config)
    if isinstance(result, offload.UnreachableHost):
        return Unreachable(result.reason)
    return target_state(target)


def format_aws_status(row: AwsStatusRow) -> str:
    from scripts.aws_target import ledger as cost_ledger, measurement
    from scripts.build_targets.targets import RemovalPending

    def at(value: float) -> str:
        return local_time(datetime.fromtimestamp(value, timezone.utc)) if value else 'unavailable'
    monthly = f'${row.monthly_usd:.2f}' if isinstance(row.monthly_usd, Decimal) else 'unavailable'
    cap = f'${row.monthly_limit_usd:.2f}' if isinstance(row.monthly_limit_usd, Decimal) else 'unavailable'
    state = f'starting since {at(row.starting_at)}' if row.state == 'starting' else row.state
    lines = [f'{row.target}: {state}; evidence {at(row.evidence_at)}; month {monthly} / {cap}',
             f'  spending {row.spending}; decision {at(row.decision_at)}; {display_field(row.reason)}']
    if isinstance(row.removal, RemovalPending):
        lines.append('  down incomplete: ' + ', '.join(row.removal.resources)
                     + f'; storage ${row.removal.storage_usd_per_day:.2f}/day; run deploy.py down --approve-destroy')
    for slots in row.slots:
        lines.append(f'  {slots.repository}: {slots.occupied}/{slots.capacity} slots; '
                     + f'{slots.active} active, {slots.retained} retained pending cleanup')
    for result in row.results:
        count = str(result.unresolved_count) if isinstance(result.unresolved_count, int) else 'unavailable'
        lines.append(f'  result {result.repo_identity} slot {result.slot}: {result.outcome}; '
                     + f'completion {result.completion}; cleanup {result.cleanup}; unresolved {count}; evidence {at(result.evidence_at)}')
    for notice in row.notices:
        text = f'  notice {notice.period}: {notice.delivery.value}'
        if notice.delivery == cost_ledger.DeliveryState.PENDING:
            text += f'; retry after {at(notice.retry_after)}'
        lines.append(text)
    if any(slots.retained for slots in row.slots):
        recovery = {
            measurement.ControllerRecovery.CONTROLLER_TICK: 'controller evidence is unavailable',
            measurement.ControllerRecovery.CONFIRM_STOP: 'controller tick confirms stop or remote cleanup',
            measurement.ControllerRecovery.STATE_EVIDENCE_RELEASE: 'state-evidence release',
        }[row.recovery]
        lines.append(f'  recovery: {recovery}; run aws_target.py tick')
    return '\n'.join(lines)


def command_status(probe: bool = False) -> int:


    from scripts.buildlog import mac_idle_alert
    from scripts.lib.user_zone import user_zone
    from scripts.message import roster
    from scripts.build_targets.targets import Held, HolderSession, Running

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
    alert = mac_idle_alert.alert_line(mac_idle_alert.state_dir() / "alert.json", time.time(), user_zone())
    if alert is not None:
        print(f"\033[1;31m{alert}\033[0m" if sys.stdout.isatty() else alert)
    for name, state in states:
        print(format_state(name, state, labels))
    return 0


def command_eligible(
    repo_root: Path, package: str, routing: Routing = ANY_ELIGIBLE_TARGET
) -> int:
    from scripts.build_targets.targets import target_eligibility

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
    platform = target.platform if isinstance(target, SshTarget) else "-"
    print(f"{target.name}\t{disposition}\t{reason}\t{settings}\t{platform}")


StepKind = Literal["check", "lint", "doc", "test", "build", "example", "final"]


@dataclass(frozen=True)
class AnyTarget:
    pass


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


def step_routing_rule(kind: StepKind, routing: Routing = ANY_ELIGIBLE_TARGET,
                      selection: TargetSelection = ROUTED, *, writes_tree: bool = False
                      ) -> AnyTarget | NatedevOnly:
    """Choose the run location for each step before target admission."""
    if writes_tree:
        return NatedevOnly("step writes the tree")
    if isinstance(routing, LocalRequired):
        return NatedevOnly(routing.reason)
    if isinstance(selection, Pinned):
        return AnyTarget()
    if kind == "build":
        return NatedevOnly("makes natedev artifacts")
    if kind == "final":
        return NatedevOnly("full gate runs on natedev")
    return AnyTarget()


def fixed_placement_constraints(
    repo_root: Path, package: str, kind: StepKind, command: tuple[str, ...],
    session: SubmittingSession, source_key: str | placement.SourceIdentity, routing: Routing = ANY_ELIGIBLE_TARGET,
    selection: TargetSelection = ROUTED, *, writes_tree: bool = False,
    provider_settings: ProviderSettings = CONFIGURED_PROVIDER_SETTINGS,
) -> placement.PlacementConstraints:
    """Keep dynamic host conditions out of the submitted candidate set."""
    from scripts.build_queue import placement
    from scripts.buildlog import store
    from scripts.mac_test import offload
    from scripts.build_targets import target_config

    rule = step_routing_rule(kind, routing, selection, writes_tree=writes_tree)
    if writes_tree:
        selection = ROUTED
    repository = offload.repository_identity(repo_root)
    candidates: list[placement.PlacementCandidate] = []
    for target in read_targets():
        eligibility: placement.AllowedHost | placement.FixedExclusion = placement.AllowedHost()
        if isinstance(target, LocalTarget):
            destination = placement.PlacementDestination("local", target.name, store.host_name(), claim_state.WHOLE_MAC)
            candidates.append(placement.PlacementCandidate(destination, eligibility))
            continue
        selected_settings: ProviderSettings = ConfiguredProviderSettings()
        if isinstance(provider_settings, ExplicitProviderSettings) and isinstance(selection, Pinned) and target.name == selection.target:
            target = replace(target, settings=provider_settings.path)
            selected_settings = provider_settings
        config = offload.read_config(target.settings)
        scope = claim_state.target_claim_scope(claim_state.state_paths(target.name), str(repo_root))
        if isinstance(rule, NatedevOnly):
            eligibility = placement.FixedExclusion(rule.reason)
        elif isinstance(repository, offload.UnknownRepository):
            eligibility = placement.FixedExclusion("repo")
        elif target_config.uses_legacy_mac_layout(target.platform):
            key = f"linux_only.{repository.name}"
            if key not in config.values:
                eligibility = placement.FixedExclusion("repo")
            else:
                excluded = offload.comma_values(config.values[key])
                if (package in excluded if kind == "test" and package
                        else offload.workspace_has_linux_only(repo_root, excluded)):
                    eligibility = placement.FixedExclusion("linux_only")
        elif not offload.linux_kind_permitted(config.values, kind):
            eligibility = placement.FixedExclusion("target does not permit this command")
        slots = (target_config.slot_capacity(config.values, repository.name,
                 missing=target_config.MissingSlotCapacity.SINGLE_SLOT)
                 if target.platform == "linux" and isinstance(repository, offload.KnownRepository) else 1)
        for slot in range(1, slots + 1):
            selected_scope = (claim_state.RepositoryScope(scope.name, scope.key, slot)
                              if target.platform == "linux" and isinstance(scope, claim_state.RepositoryScope) else scope)
            destination = placement.PlacementDestination("mac" if target.platform == "macos" else "ssh",
                target.name, config.host, selected_scope, slot if target.platform == "linux" else 0)
            candidates.append(placement.PlacementCandidate(destination, eligibility, selected_settings))
    source = placement.SourceKeyKnown(source_key) if isinstance(source_key, str) else source_key
    return placement.PlacementConstraints(source, command, str(repo_root), package, session, tuple(candidates),
        placement.PinnedHost(selection.target) if isinstance(selection, Pinned) else placement.ConfiguredOrder())


def command_constraints(args: CliArguments) -> int:
    """Print fixed candidates without probing or selecting a host."""
    from scripts.mac_test import offload
    from scripts.build_queue import placement
    from scripts.lib.atomic_file import write_text_atomic
    from scripts.build_targets import target_config

    if args.target and args.target != "natedev":
        try:
            _ = target_config.configured_ssh_target(args.target)
        except ConfigError as error:
            print(f"build targets: {error}", file=sys.stderr)
            return 2

    session = claim_state.submitting_session(args.session)
    routing = LocalRequired(args.local_reason) if args.local_reason else ANY_ELIGIBLE_TARGET
    selection = Pinned(args.target or "mac") if args.target or args.mac_only else ROUTED
    rule = step_routing_rule(cast(StepKind, args.kind), routing, selection)
    constraints = fixed_placement_constraints(Path(args.repo_root).resolve(), args.package,
        cast(StepKind, args.kind), (), session, placement.SourceKeyUnavailable("source is not submitted yet"), routing, selection)
    print("any_target" if isinstance(rule, AnyTarget) else f"natedev_only {rule.reason}")
    shown: set[str] = set()
    for candidate in constraints.candidates:
        destination = candidate.destination
        if destination.target in shown:
            continue
        shown.add(destination.target)
        target = next(target for target in read_targets() if target.name == destination.target)
        if args.mac_only and (not isinstance(target, SshTarget) or target.platform != "macos"):
            continue
        reason = (candidate.eligibility.reason if isinstance(candidate.eligibility, placement.FixedExclusion)
                  else "pinned to another target" if isinstance(selection, Pinned) and target.name != selection.target else "")
        print_eligible_target(ExcludedTarget(target, reason) if reason else EligibleTarget(target))
    if args.remote_kinds_file:
        write_text_atomic(Path(args.remote_kinds_file), "".join(
            f"{remote_verb(cast(StepKind, step))}\t{step}\n" for step in offload.remote_step_kinds(args.kind)))
    return 0


def remote_verb(kind: StepKind) -> offload.MacVerb:

    from scripts.mac_test import offload

    step = offload.remote_step_kinds(kind)[0]
    return cast(offload.MacVerb, "nextest" if step == "test" else "clippy" if step in ("lint", "example") else step)


def start_probe_recovery(target: SshTarget, repo_root: Path, package: str, kind: StepKind) -> None:
    """Start one recovery probe without delaying the eligibility answer."""

    from scripts.mac_test import offload

    directory = offload.state_directory(target.name)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "recovery-probe.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        arguments = [str(Path(__file__).parents[1] / "lib/py"), str(Path(__file__)), "recover-probe",
                     "--target", target.name, "--repo-root", str(repo_root), "--package", package,
                     "--kind", kind, "--recovery-fd", str(lock.fileno())]
        os.set_inheritable(lock.fileno(), True)
        try:
            with open(os.devnull, "r+") as output:
                _ = os.posix_spawn(arguments[0], arguments, dict(os.environ), setsid=True,
                    file_actions=tuple((os.POSIX_SPAWN_DUP2, output.fileno(), fd) for fd in (0, 1, 2)))
        finally:
            os.set_inheritable(lock.fileno(), False)


def command_recover_probe(args: CliArguments) -> int:
    """Copy scripts and probe under the normal target claim."""

    from scripts.build_targets import target_config
    from scripts.copies import copies
    from scripts.mac_test import mac_test, offload

    with os.fdopen(args.recovery_fd):
        target = target_config.configured_ssh_target(args.target)
        config = ssh_target_config(target)
        if isinstance(config, ExcludedTarget):
            return 0
        repository = offload.repository_identity(Path(args.repo_root))
        if isinstance(repository, offload.UnknownRepository):
            return 0
        paths = mac_test.state_paths(target.name)
        if mac_test.free_now(paths, target.settings, Path(args.repo_root)) != "bad":
            return 0
        request = offload.OffloadRequest(
            Path(args.repo_root), args.package, f"probe-{os.getpid()}", offload.NoResultFile(), (),
            owner="verify", holder_session=offload.holder_session(), target=target.name,
            verb=remote_verb(cast(StepKind, args.kind)), kind=args.kind)
        controller = offload.ProcessController()
        try:
            with offload.interrupt_handlers(controller):
                with ExitStack() as leases:
                    slots: list[int] = []
                    capacity = (1 if target.platform == "macos" else target_config.slot_capacity(
                        config.values, repository.name, missing=target_config.MissingSlotCapacity.SINGLE_SLOT))
                    for slot in range(1, capacity + 1):
                        try:
                            leases.enter_context(copies.remote_lease(offload.request_mirror(
                                config, replace(request, slot=slot), repository)))
                        except copies.BusyRemoteMirror:
                            continue
                        slots.append(slot)
                    if not slots:
                        return 0
                    claim = offload.claim_for_run(controller, config, request, repository, tuple(slots))
                    if not isinstance(claim, offload.FreshMacClaim):
                        return 0
                    try:
                        if not offload.copy_tracked_scripts(controller, config):
                            return 0
                        selected = replace(request, slot=claim.slot)
                        copied = offload.copy_tree_with_key(controller, config, selected, repository)
                        if isinstance(copied, offload.TreeCopyFailed):
                            return 0
                        _ = offload.probe_mac(controller, config, repository, target.name, claim.slot,
                                              record_version_decline=False)
                    finally:
                        _ = offload.release_claim(controller, target.name)
        except offload.OffloadInterrupted as interrupted:
            return 128 + interrupted.signal_number

    return 0


@dataclass(frozen=True)
class CapacityNotRead:
    """The direct eligibility caller has not read local capacity."""


CAPACITY_NOT_READ = CapacityNotRead()


def mac_target_eligibility(target: LocalTarget | SshTarget, repo_root: Path,
                           package: str, kind: StepKind, routing: Routing, *,
                           answer: mac_test.FreeNowAnswer | CapacityNotRead = CAPACITY_NOT_READ,
                           session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
                           ) -> EligibleTarget | ExcludedTarget:

    from scripts.mac_test import mac_test, offload

    session = claim_state.submitting_session(session)
    if isinstance(routing, LocalRequired):
        return ExcludedTarget(target, routing.reason)
    try:
        config = offload.read_config(target.settings) if isinstance(target, SshTarget) else offload.read_config()
        name = target.name if isinstance(target, SshTarget) else "mac"
        steps = offload.remote_step_kinds(kind)
        eligibility = offload.mac_step_eligibility(config, repo_root, package,
            remote_verb(cast(StepKind, steps[0])), name, kind=steps[0])
        if isinstance(eligibility, offload.IneligibleMacStep):
            return ExcludedTarget(target, eligibility.reason)
        for step in steps[1:]:
            policy = offload.mac_step_eligibility(config, repo_root, package,
                remote_verb(cast(StepKind, step)), name, kind=step)
            if isinstance(policy, offload.IneligibleMacStep):
                return ExcludedTarget(target, policy.reason)
        if isinstance(answer, CapacityNotRead):
            answer = mac_test.free_now(mac_test.state_paths(name), config.source, repo_root, session=session)
        if answer == "blocked":
            return ExcludedTarget(target, answer)
        if answer == "bad":
            return EligibleTarget(target)
        controller = offload.ProcessController()
        probe = offload.probe_mac(controller, config, eligibility, name)
        if isinstance(probe, offload.UnresolvedSlotStartup):
            return ExcludedTarget(target, "busy")
        if isinstance(probe, offload.UnavailableProbe):
            return ExcludedTarget(target, "unreachable")
        if isinstance(probe, offload.InterruptedTargetStatus):
            return ExcludedTarget(target, "busy")
        handlers = (offload.interrupt_handlers(controller)
                    if threading.current_thread() is threading.main_thread() else nullcontext())
        with handlers:
            disk = offload.ensure_mac_disk_floor(controller, config, eligibility, probe, name)
        if isinstance(disk, offload.SweepFailed):
            print(f"build targets: Mac disk exclusion: {disk.cause}", file=sys.stderr)
        if not isinstance(disk, offload.MacDiskReady):
            return ExcludedTarget(target, "disk")
    except offload.OffloadInterrupted as interrupted:
        raise SystemExit(128 + interrupted.signal_number) from None
    except (OSError, UnicodeError, ValueError) as error:
        return ExcludedTarget(target, f"state unreadable: {error}")
    return EligibleTarget(target)


def ssh_target_eligibility(target: SshTarget, repo_root: Path, package: str,
                           kind: StepKind, routing: Routing, *, selection: TargetSelection = ROUTED, new_step: bool = False,
                           session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION,
                           ) -> EligibleTarget | ExcludedTarget:
    """Read Linux policy and capacity without evaluating a development shell."""

    from scripts.build_targets import target_config
    from scripts.mac_test import mac_test, offload

    session = claim_state.submitting_session(session)
    if target.platform == "macos" or isinstance(routing, LocalRequired):
        answer = mac_test.free_now(mac_test.state_paths(target.name), target.settings, repo_root, session=session)
        if new_step and not isinstance(selection, Pinned) and answer == "bad":
            start_probe_recovery(target, repo_root, package, kind)
        if target.platform == "macos":
            return mac_target_eligibility(target, repo_root, package, kind, routing, answer=answer, session=session)
        return ExcludedTarget(target, cast(LocalRequired, routing).reason)
    config = ssh_target_config(target)
    if isinstance(config, ExcludedTarget):
        return config
    for step in offload.remote_step_kinds(kind):
        policy = offload.mac_step_eligibility(config, repo_root, package,
            remote_verb(cast(StepKind, step)), target.name, kind=step)
        if isinstance(policy, offload.IneligibleMacStep):
            return ExcludedTarget(target, policy.reason)
    paths = mac_test.state_paths(target.name)
    if isinstance(selection, Pinned) and target_config.is_aws_target(target):
        control = mac_test.read_json_object(paths.directory / "controller.json") or {}
        if control.get("state") in ("stopped", "starting", "pending", "stopping"):
            return ExcludedTarget(target, "starting")
        if control.get("state") == "refused":
            return ExcludedTarget(target, str(control.get("reason", "controller refused")))
    answer = mac_test.free_now(paths, target.settings, repo_root, session=session)
    if new_step and not isinstance(selection, Pinned) and answer == "bad":
        start_probe_recovery(target, repo_root, package, kind)
    if answer not in ("free", "bad"):
        return ExcludedTarget(target, answer)
    if answer == "bad":
        return EligibleTarget(target)
    if isinstance(offload.probe_reachability(offload.ProcessController(), config), offload.UnreachableHost):
        return ExcludedTarget(target, "unreachable")
    return EligibleTarget(target)


def aws_spending_exclusion(target: SshTarget) -> str:
    """Check AWS spending before reading the execution settings."""
    from scripts.build_targets import target_config

    if not target_config.is_aws_target(target):
        return ""
    return aws_target_spending_exclusion(target)


def aws_target_spending_exclusion(target: SshTarget) -> str:
    """Read spending permission for a configured AWS target."""

    from scripts.aws_target import config as aws_config
    from scripts.lib import conf

    if not aws_config.offload_enabled(conf.read(target.settings)):
        return "off"
    from scripts.aws_target import ledger as cost_ledger
    from scripts.mac_test import mac_test

    decision = cost_ledger.spending_guard(target.settings, mac_test.state_paths(target.name).directory)
    if isinstance(decision, cost_ledger.SpendingAllowed):
        return ""
    print(f"build targets: {target.name}: spending refused: {decision.reason}", file=sys.stderr)
    return "spending_limit" if isinstance(decision, cost_ledger.SpendingRefused) else "spending_unavailable"


def ssh_target_config(target: SshTarget) -> offload.OffloadConfig | ExcludedTarget:
    """Exclude unusable AWS settings without stopping selection for other targets."""

    from scripts.build_targets import target_config
    from scripts.lib import conf
    from scripts.mac_test import offload

    try:
        reason = aws_spending_exclusion(target)
        if reason:
            return ExcludedTarget(target, reason)
        return offload.read_config(target.settings)
    except conf.Unusable as error:
        if not target_config.is_aws_target(target):
            raise
        print(f"build targets: {target.name}: spending refused: {error}", file=sys.stderr)
        return ExcludedTarget(target, "spending_unavailable")


def command_pinned_eligible(args: CliArguments, routing: Routing) -> int:
    """Use normal eligibility, but wait for stopped AWS capacity before its reachability check."""

    from scripts.build_targets import target_config

    try:
        if args.target == "natedev":
            local = local_target()
            print("natedev_only pinned to natedev")
            print_eligible_target(EligibleTarget(local))
            return 0
        target = target_config.configured_ssh_target(args.target)
        kind = cast(StepKind, args.kind)
        result = ssh_target_eligibility(target, Path(args.repo_root), args.package, kind, routing,
                                        selection=Pinned(target.name), new_step=args.new_step)
        if isinstance(result, ExcludedTarget) and result.reason not in ("busy", "blocked", "starting"):
            raise ValueError(f"target {target.name} refused: {result.reason}")
        print("any_target")
        print_eligible_target(result)
        return 0
    except (ConfigError, OSError, UnicodeError, ValueError) as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2


def wait_claim_identity(paths: mac_test.StatePaths, repo_root: str, pid: int,
                    *, session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> claim_state.ClaimIdentity | claim_state.NoClaimIdentity:
    """Find the exact reservation for this process and session."""
    from scripts.lib import process_start as process_identity
    from scripts.mac_test import mac_test

    session = claim_state.submitting_session(session)
    start = process_identity.process_start(pid)
    if not isinstance(start, process_identity.Started):
        return claim_state.NO_CLAIM_IDENTITY
    try:
        scope = mac_test.target_claim_scope(paths, repo_root)
        block = mac_test.blocking_claim_block(paths, session)
        return next((claim_state.claim_identity(claim.record) for claim in mac_test.read_claims(paths)
                     if mac_test.scopes_overlap(claim.scope, scope)
                     and claim.record["pid"] == pid and claim.record["proc_start"] == start.start
                     and claim.record["submitting_session"] == claim_state.session_id(session)
                     and ("remote_host" not in claim.record or claim.record.get("cleanup_pending"))
                     and (isinstance(block, mac_test.NoMacBlock) or "remote_host" in claim.record)),
                    claim_state.NO_CLAIM_IDENTITY)
    except (OSError, mac_test.StateReadError):
        return claim_state.NO_CLAIM_IDENTITY


def cleanup_refused_claims(target: SshTarget, args: CliArguments, *, session: SubmittingSession) -> None:
    """Recover retained cleanup ownership without granting permission to execute."""

    from scripts.lib import conf, process_start as process_identity
    from scripts.mac_test import mac_test, offload

    paths = mac_test.state_paths(target.name)
    scope = mac_test.target_claim_scope(paths, args.repo_root)
    if not isinstance(scope, mac_test.RepositoryScope):
        return
    start = process_identity.process_start(args.pid)
    if not isinstance(start, process_identity.Started):
        return
    with mac_test.locked(paths.lock):
        claims = tuple(claim for claim in mac_test.read_claims(paths)
                       if mac_test.scopes_overlap(claim.scope, scope) and "remote_host" in claim.record
                       and (mac_test.run_is_kept(claim.record)
                            or (claim.record.get("cleanup_pending") and claim.record["pid"] == args.pid
                                and claim.record["proc_start"] == start.start)))
    if not claims:
        return
    try:
        config = offload.read_connection_settings(target.settings)
    except conf.Unusable as error:
        print(f"build targets: {target.name}: cleanup settings unavailable: {error}", file=sys.stderr)
        return
    owner = mac_test.CliArguments()
    owner.pid = args.pid
    for claim in claims:
        with mac_test.locked(paths.lock):
            if mac_test.read_run(claim.path) != claim.record or "remote_host" not in claim.record:
                continue
            with redirect_stdout(io.StringIO()):
                attempt = mac_test.take_over_kept_claim(owner, claim, claim.record, session=session)
            if not isinstance(attempt, mac_test.ClaimFinished) or attempt.status != 13:
                continue
        try:
            checked = offload.check_leftovers(config, claim.record["remote_host"],
                                              offload.KnownRepository(scope.name, scope.key), target.name,
                                              claim.record.get("slot", 1))
        except conf.Unusable as error:
            print(f"build targets: {target.name}: cleanup settings unavailable: {error}", file=sys.stderr)
            continue
        if isinstance(checked, offload.LeftoversEnded):
            release = mac_test.CliArguments()
            release.pid = args.pid
            release.claim_id = claim.record["claim_id"]
            release.episode = claim.record["episode"]
            release.attempt = claim.record["attempt"]
            release.settings = str(target.settings)
            _ = mac_test.command_release(release, paths)


def publish_wait_reservation(args: CliArguments, target: SshTarget,
                             identity: claim_state.ClaimIdentity | claim_state.NoClaimIdentity,
                             session: SubmittingSession) -> None:
    """Publish the target and the exact identity for release and handoff."""
    from scripts.lib.atomic_file import write_text_atomic

    write_text_atomic(Path(args.reservation_file), f"{target.name}\t{target.settings}\n")
    if args.claim_identity_file and isinstance(identity, claim_state.ClaimIdentity):
        write_text_atomic(Path(args.claim_identity_file),
                          f"{identity.claim_id}\t{identity.episode}\t{identity.attempt}\t{claim_state.session_id(session)}\n")


def reserve_assigned(args: CliArguments) -> int:
    """Claim only the target, scope and slot in the saved assignment."""
    from scripts.build_queue import admission_protocol as wire, placement
    from scripts.mac_test import mac_test
    from scripts.build_targets import target_config

    saved = wire.parse_reply(Path(args.assignment_file).read_text())
    if not isinstance(saved, wire.Assignment):
        raise ValueError("reservation requires a remote assignment")
    assignment = saved.assignment
    target = target_config.configured_ssh_target(assignment.destination.target)
    paths = mac_test.state_paths(target.name)
    if args.recover_only:
        from scripts.lib import process_start

        try:
            with mac_test.locked(paths.lock):
                expected = claim_state.ClaimIdentity(uuid.uuid5(uuid.NAMESPACE_URL,
                    placement.attempt_key(assignment.episode, assignment.attempt)).hex,
                    assignment.episode, assignment.attempt)
                owner = process_start.process_start(args.pid)
                if isinstance(owner, process_start.Unreadable):
                    return 78
                matches = [held for held in mac_test.read_claims(paths)
                    if held.scope == assignment.destination.scope and held.record["pid"] == args.pid
                    and owner == process_start.Started(held.record["proc_start"])
                    and held.record["submitting_session"] == claim_state.session_id(assignment.session)
                    and (claim_state.claim_identity(held.record) == expected
                         or "remote_host" in held.record and held.record.get("cleanup_pending"))]
                if not matches:
                    return 1
                if len(matches) != 1:
                    return 78
                held = matches[0]
                publish_wait_reservation(args, target, claim_state.claim_identity(held.record), assignment.session)
                return 13 if "remote_host" in held.record else 0
        except (OSError, ValueError, mac_test.StateReadError):
            return 78
    identity = wait_claim_identity(paths, args.repo_root, args.pid, session=assignment.session)
    if (isinstance(identity, claim_state.ClaimIdentity)
            and (identity.episode, identity.attempt) == (assignment.episode, assignment.attempt)):
        publish_wait_reservation(args, target, identity, assignment.session)
        return 0
    claim = mac_test.CliArguments()
    claim.pid, claim.target, claim.repo_root = args.pid, target.name, args.repo_root
    claim.package, claim.verb, claim.settings = args.package, remote_verb(cast(StepKind, args.kind)), str(target.settings)
    claim.identity = True
    with redirect_stdout(io.StringIO()) as output:
        status = mac_test.command_claim_wait(claim, paths, kind=args.kind,
                                             session=assignment.session, assignment=assignment)
    identity = claim_state.reply_claim_identity(output.getvalue())
    if status not in (0, 13):
        return status
    publish_wait_reservation(args, target, identity, assignment.session)
    return status


def command_reserve(args: CliArguments) -> int:
    """Reserve before a queue leave; the wait process owns the reservation."""

    from scripts.build_targets import target_config

    if args.assignment_file:
        return reserve_assigned(args)
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    destination = Path(args.reservation_file)
    if destination.exists():
        name, separator, settings = destination.read_text().strip().partition("\t")
        if separator and (not args.target or name == args.target):
            target = target_config.configured_ssh_target(name)
            if settings != str(target.settings):
                destination.unlink()
                return 1
            config = ssh_target_config(target)
            if not isinstance(config, ExcludedTarget):
                if config.enabled:
                    from scripts.mac_test import mac_test

                    paths = mac_test.state_paths(name)
                    identity = wait_claim_identity(paths, args.repo_root, args.pid, session=session)
                    if isinstance(identity, claim_state.ClaimIdentity):
                        publish_wait_reservation(args, target, identity, session)
                        return 0
        destination.unlink(missing_ok=True)
    if args.kind not in ("test", "check", "doc", "lint", "example", "build", "final"):
        return 1
    busy_mac = False
    for target in read_targets():
        if not isinstance(target, SshTarget) or (args.target and target.name != args.target):
            continue
        from scripts.mac_test import mac_test, offload

        verb = remote_verb(args.kind)
        config = ssh_target_config(target)
        if isinstance(config, ExcludedTarget):
            if config.reason != "off":
                cleanup_refused_claims(target, args, session=session)
            continue
        if not config.enabled:
            continue
        paths = mac_test.state_paths(target.name)
        identity = wait_claim_identity(paths, args.repo_root, args.pid, session=session)
        if isinstance(identity, claim_state.NoClaimIdentity):
            answer = mac_test.free_now(paths, target.settings, Path(args.repo_root), session=session)
            if answer not in ("free", "busy") and not (answer == "bad" and args.new_step):
                continue
        policy = offload.mac_step_eligibility(config, Path(args.repo_root), args.package, verb, target.name, kind=args.kind)
        if isinstance(policy, offload.IneligibleMacStep):
            continue
        if isinstance(identity, claim_state.ClaimIdentity):
            publish_wait_reservation(args, target, identity, session)
            return 0
        if args.recover_only:
            scope = mac_test.target_claim_scope(paths, args.repo_root)
            with mac_test.locked(paths.lock):
                retained = any(mac_test.scopes_overlap(claim.scope, scope)
                               and mac_test.run_is_kept(claim.record)
                               for claim in mac_test.read_claims(paths))
            if not retained:
                busy_mac = busy_mac or target.platform == "macos"
                continue
        if isinstance(offload.probe_reachability(offload.ProcessController(), config), offload.UnreachableHost):
            continue
        claim = mac_test.CliArguments()
        claim.pid, claim.target, claim.repo_root = args.pid, target.name, args.repo_root
        claim.settings, claim.package, claim.verb = str(target.settings), args.package, verb
        claim.identity = True
        with redirect_stdout(io.StringIO()) as output:
            status = mac_test.command_claim_wait(claim, paths, kind=args.kind, session=session)
        identity = claim_state.reply_claim_identity(output.getvalue())
        if status not in (0, 13):
            if status == 11 and target.platform == "macos":
                busy_mac = True
            continue
        try:
            publish_wait_reservation(args, target, identity, session)
        except BaseException:
            _ = command_release_wait(args.pid, identity=identity, session=session)
            raise
        return 0
    return 11 if busy_mac else 1


def command_release_wait(pid: int, *,
                         identity: claim_state.ClaimIdentity | claim_state.NoClaimIdentity = claim_state.NO_CLAIM_IDENTITY,
                         session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:
    """Release only process-bound reservations; remote claims retain ownership."""
    from scripts.lib import process_start as process_identity

    session = claim_state.submitting_session(session)
    start = process_identity.process_start(pid)
    if not isinstance(start, process_identity.Started):
        return 0
    for target in read_targets():
        if not isinstance(target, SshTarget):
            continue
        from scripts.build_targets import target_config

        if not target_config.ssh_target_state_directory(target.name).exists():
            continue
        paths = claim_state.state_paths(target.name)
        fenced: list[claim_state.HeldClaim] = []
        with claim_state.locked(paths.lock):
            for claim in claim_state.read_claims(paths):
                if (claim.record["pid"] == pid and claim.record["proc_start"] == start.start
                        and claim.record["submitting_session"] == claim_state.session_id(session)
                        and claim_state.claim_identity(claim.record) == identity
                        and "remote_host" not in claim.record):
                    from scripts.mac_test import offload

                    exact = claim_state.ClaimIdentity(claim.record["claim_id"], claim.record["episode"], claim.record["attempt"])
                    if offload.pending_start_path(target.name, exact).exists():
                        fenced.append(claim)
                    else:
                        claim.path.unlink(missing_ok=True)
        for claim in fenced:
            from scripts.mac_test import mac_test

            release = mac_test.CliArguments()
            release.pid, release.target, release.settings = pid, target.name, str(target.settings)
            release.claim_id = claim.record["claim_id"]
            release.episode, release.attempt = claim.record["episode"], claim.record["attempt"]
            status = mac_test.command_release(release, paths)
            if status != 0:
                return status
    return 0


def command_kind_eligible(repo_root: Path, package: str, kind: StepKind, routing: Routing,
                          *, mac_only: bool = False, new_step: bool = False) -> int:
    decision = step_routing_rule(kind, routing, Pinned("mac") if mac_only else ROUTED)
    try:
        eligibility = tuple(
            ExcludedTarget(target, decision.reason) if isinstance(target, SshTarget) and isinstance(decision, NatedevOnly) and not new_step
            else ssh_target_eligibility(target, repo_root, package, kind,
                LocalRequired(decision.reason) if isinstance(decision, NatedevOnly) else routing,
                selection=Pinned(target.name) if mac_only else ROUTED, new_step=new_step)
            if isinstance(target, SshTarget) else EligibleTarget(target)
            for target in read_targets()
            if not mac_only or isinstance(target, SshTarget) and target.platform == "macos"
        )
    except (ConfigError, OSError, UnicodeError, ValueError) as error:
        print(f"natedev_only state unreadable: {display_field(str(error))}")
        return 0
    print("any_target" if isinstance(decision, AnyTarget)
          else f"natedev_only {display_field(decision.reason)}")
    for result in eligibility:
        print_eligible_target(result)
    return 0


def wait_clock() -> datetime:

    from scripts.build_hold import build_hold

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
    import subprocess

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


@dataclass(frozen=True)
class NoBlockingHold:
    """No hold in force prevents this session from starting."""


@dataclass(frozen=True)
class ConfirmedCallerLineage:
    """The caller identity matches the checked process lineage."""

    processes: frozenset[build_hold.ProcessIdentity]


@dataclass(frozen=True)
class UnavailableCallerLineage:
    """The caller lineage cannot grant permission through a keep list."""

    reason: str


CallerLineage = ConfirmedCallerLineage | UnavailableCallerLineage


def read_caller_lineage(proc_root: Path, pid: int, start_time: str) -> CallerLineage:
    """Read the lineage only while the checked caller identity matches."""
    from scripts.build_hold import build_hold

    if not start_time or build_hold.process_start_time(pid, proc_root) != start_time:
        return UnavailableCallerLineage("the caller start time is unavailable or differs")
    processes = build_hold.caller_processes(proc_root, pid)
    if (build_hold.ProcessIdentity(pid, start_time) not in processes
            or build_hold.process_start_time(pid, proc_root) != start_time):
        return UnavailableCallerLineage("the caller lineage is unavailable or its identity changed")
    return ConfirmedCallerLineage(processes)


def current_caller_lineage() -> CallerLineage:
    """Read the current process lineage for a local hold decision."""
    from scripts.build_hold import build_hold

    pid = os.getpid()
    return read_caller_lineage(build_hold.PROC_ROOT, pid, build_hold.process_start_time(pid, build_hold.PROC_ROOT))


def hold_waited_on(holds: build_hold.NatedevHolds,
                   owner: OwningSession | NoOwningSession,
                   directory: Path, caller_lineage: CallerLineage) -> build_hold.Holder | NoBlockingHold:
    """Choose the oldest hold that applies to the caller and belongs to another session."""
    from scripts.build_hold import build_hold

    own_key = ""
    if isinstance(owner, OwningSession):
        try:
            own_key = build_hold.holder_path(directory, build_hold.holder_of(owner.session_id).key).name
        except ValueError:
            pass
    for holder in holds.in_force:
        kept = (isinstance(caller_lineage, ConfirmedCallerLineage)
                and bool(caller_lineage.processes.intersection(holder.keep)))
        if holder.key != own_key and not kept:
            return holder
    return NoBlockingHold()


def caller_waits_on_hold(holds: build_hold.NatedevHolds, directory: Path) -> bool:
    """Check whether a hold makes the calling session wait."""
    from scripts.build_hold import build_hold

    return isinstance(hold_waited_on(holds, build_hold.owning_session(), directory,
                                    current_caller_lineage()), build_hold.Holder)


def command_register_local(_call_id: str, *, pid: int,
                           session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:
    """Check the local hold without waiting or writing a call record."""
    _ = pid
    return command_check_local("verify", session=session)


def tell_wait_holder(holder: build_hold.Holder, line: str, caller: str) -> None:
    """Tell the holder about unresolved ownership without ending the wait."""
    import subprocess

    from scripts.build_hold import build_hold
    from scripts.lib import conf
    from scripts.lib.process_failure import failure_detail
    from scripts.message import session_send

    try:
        with conf.diagnostics(report=False):
            timeout = conf.read("build_targets").number("hold_sender_timeout_seconds")
        result = subprocess.run(session_send.send_argv("--to", build_hold.holder_address(holder.key), "--text", line),
                                capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as error:
        reason = f"send timed out after {error.timeout:g} seconds"
    except (OSError, conf.Unusable) as error:
        reason = str(error)
    else:
        if result.returncode == 0:
            return
        reason = failure_detail(result)
    print(f"{speaker(caller)}: could not tell {display_field(holder.name)}: {display_field(reason)}",
          file=sys.stderr, flush=True)


def command_wait_local(call_id: str, *, pid: int = 0, leave_hook: str = "", caller: str = "",
                       session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:
    """Wait until no hold applies. An empty caller is verify.sh; any other caller is a lint step."""

    from scripts.build_hold import build_hold
    from scripts.build_targets.targets import Held, StateUnreadable

    # This CLI's import root excludes the worker module.
    # Use importlib at the typed provider boundary.
    release_waiter = cast(Callable[[Path, Path], LocalHoldWait], getattr(
        importlib.import_module("scripts.direct.local_release_wait"), "local_release_waiter"))
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
            waiter: ActiveWaitProcess = {"call_id": call_id, "pid": os.getpid(),
                                         "proc_start": build_hold.process_start_time(os.getpid())}
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
            directory = build_hold.holder_directory()
            holds = build_hold.natedev_holds(directory)
            if not holds.in_force:
                return 0
            session = claim_state.submitting_session(session)
            blocking = hold_waited_on(holds, session, directory, current_caller_lineage())
            if isinstance(blocking, NoBlockingHold):
                return 0
            if not isinstance(state, Held):
                print(wait_diagnostic("holder file exists; target state disagrees", caller), file=sys.stderr, flush=True)
                return 12
            if not announced:
                announced = True
                print(f"{name}: natedev is held by {display_field(blocking.name)} for "
                      + f"{display_field(blocking.purpose)}{waiting}",
                      file=sys.stderr, flush=True)
                resolution = build_hold.run_resolution()
                if isinstance(resolution, build_hold.RunNotInStaff):
                    line = (f"{name}: build has no run pointer ({display_field(str(resolution.run))}); "
                            + f"waits on {display_field(blocking.name)} hold for {display_field(blocking.purpose)}.")
                elif isinstance(session, NoOwningSession):
                    line = (f"{name}: build has no session; waits on {display_field(blocking.name)} hold "
                            + f"for {display_field(blocking.purpose)}.")
                else:
                    line = ""
                if line:
                    print(line, file=sys.stderr, flush=True)
                    tell_wait_holder(blocking, line, caller)
            # A held natedev cannot start this step, so a free Mac wins and the
            # call can leave without an allocation. As in memory_gate.sh, the
            # hook runs from the second poll on: a Mac that declines after
            # free-now said free costs one poll interval, never a tight loop.
            if polled and leave_hook_says_free(leave_hook):
                print(f"{name}: the Mac is free: leaving natedev's hold wait to run there.",
                      file=sys.stderr, flush=True)
                return LEFT_QUEUE_STATUS
            if not escalated and (wait_clock() - blocking.since).total_seconds() >= escalation:
                print(f"{name}: natedev has been held for 2 h by {display_field(blocking.name)}; ask them.",
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

    from scripts.build_hold import build_hold

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


def command_check_local(caller: str = "", *,
                        session: SubmittingSession | claim_state.CallingSession = claim_state.CALLING_SESSION) -> int:

    from scripts.build_hold import build_hold
    from scripts.build_targets.targets import Held, StateUnreadable

    try:
        state = target_state(local_target())
        output = sys.stderr if caller else sys.stdout
        if isinstance(state, StateUnreadable):
            print(wait_diagnostic(state.reason, "" if caller == "verify" else caller), file=output)
            return 12
        directory = build_hold.holder_directory()
        holds = build_hold.natedev_holds(directory)
        if not holds.in_force:
            return 0
        owner = claim_state.submitting_session(session)
        blocking = hold_waited_on(holds, owner, directory, current_caller_lineage())
        if isinstance(blocking, NoBlockingHold):
            return 0
        if not isinstance(state, Held):
            print(wait_diagnostic("holder file exists; target state disagrees", caller), file=output)
            return 12
        if caller:
            label = speaker("" if caller == "verify" else caller)
            print(f"{label}: natedev is held by {display_field(blocking.name)} for {display_field(blocking.purpose)}.", file=output)
            escalation = float(os.environ.get("BUILD_TARGETS_ESCALATE_S", "7200"))
            if (wait_clock() - blocking.since).total_seconds() >= escalation:
                print(f"{label}: natedev has been held for 2 h by {display_field(blocking.name)}; ask them.", file=output)
        else:
            if isinstance(owner, OwningSession):
                _ = build_hold.register_wait(owner.session_id)
            print(f"natedev is held by {display_field(blocking.name)} for {display_field(blocking.purpose)}; launch after the release.")
        return 10
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error), "" if caller == "verify" else caller), file=sys.stderr if caller else sys.stdout)
        return 12


def command_eligibility(args: CliArguments) -> int:
    """Apply eligibility flags without loading them for other commands."""

    from scripts.lib.atomic_file import write_text_atomic
    if args.probe_marks_file:
        from scripts.mac_test import offload

        repository = offload.repository_identity(Path(args.repo_root))
        marks: list[str] = []
        for target in read_targets():
            if isinstance(target, SshTarget):
                marks.append(str(offload.state_directory(target.name) / "failed_probe"))
                if isinstance(repository, offload.KnownRepository):
                    marks.append(str(offload.linux_version_decline_path(target.name, repository.name)))
        write_text_atomic(Path(args.probe_marks_file), "".join(path + "\n" for path in marks))
    if args.remote_kinds_file:
        from scripts.mac_test import offload

        write_text_atomic(Path(args.remote_kinds_file), "".join(
            f"{remote_verb(cast(StepKind, step))}\t{step}\n"
            for step in offload.remote_step_kinds(args.kind)))
    routing = LocalRequired(args.local_reason) if args.local_reason else ANY_ELIGIBLE_TARGET
    if args.target:
        return command_pinned_eligible(args, routing)
    if args.kind:
        return command_kind_eligible(Path(args.repo_root).resolve(), args.package, cast(StepKind, args.kind), routing, mac_only=args.mac_only, new_step=args.new_step)
    return command_eligible(Path(args.repo_root).resolve(), args.package, routing)


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "build_targets/build_targets.py")
    from scripts.build_targets import target_config

    args = parser().parse_args(namespace=CliArguments())
    if args.command == "recover-probe":
        return command_recover_probe(args)
    if args.command == "source-key":
        from scripts.buildlog import treekey

        print(treekey.tree_key(args.repo_root) or "")
        return 0
    if args.command == "submitting-session":
        print(claim_state.session_id(claim_state.submitting_session(args.session)))
        return 0
    if args.command == "assigned-info":
        from scripts.build_queue import admission_protocol as wire

        saved = wire.parse_reply(Path(args.assignment_file).read_text())
        if not isinstance(saved, (wire.Assignment, wire.LocalPlacementGranted)):
            raise wire.AdmissionUnavailable("file contains no assigned attempt")
        destination = saved.assignment.destination
        if destination.provider == "local":
            print("-\tlinux\t" + claim_state.session_id(saved.assignment.session))
        else:
            target = target_config.configured_ssh_target(destination.target)
            print(f"{target.settings}\t{target.platform}\t{claim_state.session_id(saved.assignment.session)}")
        return 0
    if args.command in ("reserve", "recover-reservation"):
        if args.command == "recover-reservation":
            args.recover_only = True
        return command_reserve(args)
    if args.command in ("release-wait", "eligible", "constraints"):
        from scripts.build_targets import target_config

        with target_config.command_configuration():
            if args.command == "release-wait":
                return command_release_wait(args.pid, identity=claim_state.ClaimIdentity(
                    args.claim_id, args.episode, args.attempt), session=args.session)
            return command_constraints(args) if args.command == "constraints" else command_eligibility(args)
    if args.command == "register-local":
        return command_register_local(args.call_id, pid=args.pid, session=args.session)
    if args.command == "wait-local":
        return command_wait_local(args.call_id, pid=args.pid, leave_hook=args.leave_hook, caller=args.caller, session=args.session)
    if args.command == "check-local":
        return command_check_local(args.caller, session=args.session)
    if args.command == "interrupt-local":
        return command_interrupt_local(args.call_id, args.signal_name)
    return command_status(args.probe)


if __name__ == "__main__":
    raise SystemExit(main())
