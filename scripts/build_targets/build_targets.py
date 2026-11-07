#!/usr/bin/env python3
"""Show the recorded state of each configured build target."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Mapping
from contextlib import ExitStack, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import FrameType
from typing import Literal, Protocol, TypedDict, cast, final

from scripts.build_hold import override_guard
from scripts.build_targets import target_config
from scripts.message import roster


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
        self.recover_only: bool = False


def positive_pid(value: str) -> int:
    pid = int(value)
    if pid <= 0:
        raise argparse.ArgumentTypeError("must be a positive PID")
    return pid


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    reserve = commands.add_parser("reserve")
    _ = reserve.add_argument("--repo-root", required=True)
    _ = reserve.add_argument("--package", default="")
    _ = reserve.add_argument("--kind", required=True)
    _ = reserve.add_argument("--pid", type=positive_pid, required=True)
    _ = reserve.add_argument("--reservation-file", required=True)
    _ = reserve.add_argument("--target", default="")
    _ = reserve.add_argument("--recover-only", action="store_true")
    release = commands.add_parser("release-wait")
    _ = release.add_argument("--pid", type=positive_pid, required=True)
    status = commands.add_parser("status")
    _ = status.add_argument("--probe", action="store_true")
    eligible = commands.add_parser("eligible")
    _ = eligible.add_argument("--repo-root", required=True)
    _ = eligible.add_argument("--package", default="")
    _ = eligible.add_argument("--kind", choices=("check", "lint", "doc", "test", "build", "example", "final"), default="")
    _ = eligible.add_argument("--local", dest="local_reason", default="")
    _ = eligible.add_argument("--target", default="")
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


from scripts.build_targets.targets import (
    ANY_ELIGIBLE_TARGET,
    AwsStatusRow,
    RemovalPending,
    read_aws_status as read_aws_status,
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
from scripts.mac_test import mac_test, offload
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.build_hold import build_hold
from scripts.lib.user_zone import user_zone
from scripts.lib import process_start as process_identity
from scripts.direct.step_record import Pinned, ROUTED, TargetSelection
from scripts.buildlog import mac_idle_alert
from scripts.aws_target import config as aws_config, ledger as cost_ledger, measurement


class ActiveWaitProcess(TypedDict):
    call_id: str
    pid: int
    proc_start: str


def local_time(instant: datetime) -> str:
    return instant.astimezone(user_zone()).strftime("%H:%M %Z")


display_field = mac_idle_alert.display_field


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
    if target_config.is_aws_target(target):
        return target_state(target)
    config = offload.read_config(target.settings)
    result = offload.probe_reachability(offload.ProcessController(), config)
    if isinstance(result, offload.UnreachableHost):
        return Unreachable(result.reason)
    return target_state(target)



def format_aws_status(row: AwsStatusRow) -> str:
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
class MacAllowed:
    pass


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


def target_can_retry(result: EligibleTarget | ExcludedTarget) -> bool:
    """Keep temporary refusals available for a later reservation."""
    return (isinstance(result, EligibleTarget)
            or result.reason in {"busy", "blocked", "disk", "backoff", "unreachable", "state", "copy", "mac_busy",
                                 "spending_limit", "spending_unavailable"}
            or result.reason.startswith("state unreadable: "))


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
        name = target.name if isinstance(target, SshTarget) else "mac"
        eligibility = offload.mac_step_eligibility(config, repo_root, package, verb, name)
        if isinstance(eligibility, offload.IneligibleMacStep):
            return ExcludedTarget(target, eligibility.reason)
        controller = offload.ProcessController()
        probe = offload.probe_mac(controller, config, eligibility, name)
        if isinstance(probe, offload.UnresolvedSlotStartup):
            return ExcludedTarget(target, "busy")
        if isinstance(probe, offload.UnavailableProbe):
            return ExcludedTarget(target, "unreachable")
        if isinstance(probe, offload.InterruptedTargetStatus):
            return ExcludedTarget(target, "busy")
        with offload.interrupt_handlers(controller):
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
                           kind: StepKind, routing: Routing, *, selection: TargetSelection = ROUTED
                           ) -> EligibleTarget | ExcludedTarget:
    """Read Linux policy and capacity without evaluating a development shell."""
    if target.platform == "macos":
        return mac_target_eligibility(target, repo_root, package, kind, routing)
    if isinstance(routing, LocalRequired):
        return ExcludedTarget(target, routing.reason)
    if kind not in ("test", "check", "doc"):
        return ExcludedTarget(target, "local kind")
    config = ssh_target_config(target)
    if isinstance(config, ExcludedTarget):
        return config
    verb: offload.MacVerb = "nextest" if kind == "test" else kind
    policy = offload.mac_step_eligibility(config, repo_root, package, verb, target.name)
    if isinstance(policy, offload.IneligibleMacStep):
        return ExcludedTarget(target, policy.reason)
    paths = mac_test.state_paths(target.name)
    if isinstance(selection, Pinned) and target_config.is_aws_target(target):
        control = mac_test.read_json_object(paths.directory / "controller.json") or {}
        if control.get("state") in ("stopped", "starting", "pending", "stopping"):
            return ExcludedTarget(target, "starting")
        if control.get("state") == "refused":
            return ExcludedTarget(target, str(control.get("reason", "controller refused")))
    answer = mac_test.free_now(paths, target.settings, repo_root)
    if answer != "free":
        return ExcludedTarget(target, answer)
    if isinstance(offload.probe_reachability(offload.ProcessController(), config), offload.UnreachableHost):
        return ExcludedTarget(target, "unreachable")
    return EligibleTarget(target)


def aws_spending_exclusion(target: SshTarget) -> str:
    """Check AWS spending before reading the execution settings."""
    if not target_config.is_aws_target(target):
        return ""
    if not aws_config.offload_enabled(conf.read(target.settings)):
        return "off"
    decision = cost_ledger.spending_guard(target.settings, mac_test.state_paths(target.name).directory)
    if isinstance(decision, cost_ledger.SpendingAllowed):
        return ""
    print(f"build targets: {target.name}: spending refused: {decision.reason}", file=sys.stderr)
    return "spending_limit" if isinstance(decision, cost_ledger.SpendingRefused) else "spending_unavailable"


def ssh_target_config(target: SshTarget) -> offload.OffloadConfig | ExcludedTarget:
    """Exclude unusable AWS settings without stopping selection for other targets."""
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
    try:
        target = target_config.configured_ssh_target(args.target)
        if args.kind not in ("test", "check", "doc"):
            raise ValueError("--target supports test, check, and doc only")
        kind = cast(StepKind, args.kind)
        result = ssh_target_eligibility(target, Path(args.repo_root), args.package, kind, routing,
                                        selection=Pinned(target.name))
        if isinstance(result, ExcludedTarget) and result.reason not in ("busy", "blocked", "starting"):
            raise ValueError(f"target {target.name} refused: {result.reason}")
        print("ssh_allowed")
        print_eligible_target(result)
        return 0
    except (ConfigError, OSError, UnicodeError, ValueError) as error:
        print(f"build targets: {error}", file=sys.stderr)
        return 2


def owns_wait_claim(paths: mac_test.StatePaths, repo_root: str, pid: int) -> bool:
    """Find the same process's reservation, including retained cleanup ownership."""
    start = process_identity.process_start(pid)
    if not isinstance(start, process_identity.Started):
        return False
    scope = mac_test.target_claim_scope(paths, repo_root)
    with mac_test.locked(paths.lock):
        block = mac_test.blocking_claim_block(paths)
        return any(mac_test.scopes_overlap(claim.scope, scope)
                   and claim.record["pid"] == pid and claim.record["proc_start"] == start.start
                   and ("remote_host" not in claim.record or claim.record.get("cleanup_pending"))
                   and (isinstance(block, mac_test.NoMacBlock) or "remote_host" in claim.record)
                   for claim in mac_test.read_claims(paths))


def cleanup_refused_claims(target: SshTarget, args: CliArguments) -> None:
    """Recover retained cleanup ownership without granting permission to execute."""
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
                attempt = mac_test.take_over_kept_claim(owner, claim, claim.record)
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
            with mac_test.locked(paths.lock):
                if mac_test.read_run(claim.path) == claim.record:
                    claim.path.unlink(missing_ok=True)


def command_reserve(args: CliArguments) -> int:
    """Reserve before a queue leave; the wait process owns the reservation."""
    destination = Path(args.reservation_file)
    if destination.exists():
        name, separator, settings = destination.read_text().strip().partition("\t")
        if separator:
            target = target_config.configured_ssh_target(name)
            if settings != str(target.settings):
                destination.unlink()
                return 1
            config = ssh_target_config(target)
            if not isinstance(config, ExcludedTarget):
                if config.enabled:
                    paths = mac_test.state_paths(name)
                    if (not args.target or name == args.target) and owns_wait_claim(paths, args.repo_root, args.pid):
                        return 0
        destination.unlink(missing_ok=True)
    if args.kind not in ("test", "check", "doc", "lint"):
        return 1
    verb: offload.MacVerb = ("nextest" if args.kind == "test" else "clippy" if args.kind == "lint"
                            else cast(offload.MacVerb, args.kind))
    busy_mac = False
    for target in read_targets():
        if not isinstance(target, SshTarget) or (args.target and target.name != args.target):
            continue
        config = ssh_target_config(target)
        if isinstance(config, ExcludedTarget):
            if config.reason != "off":
                cleanup_refused_claims(target, args)
            continue
        policy = offload.mac_step_eligibility(config, Path(args.repo_root), args.package, verb, target.name)
        if isinstance(policy, offload.IneligibleMacStep):
            continue
        paths = mac_test.state_paths(target.name)
        if owns_wait_claim(paths, args.repo_root, args.pid):
            write_text_atomic(destination, f"{target.name}\t{target.settings}\n")
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
        if mac_test.free_now(paths, target.settings, Path(args.repo_root)) not in ("free", "busy"):
            continue
        if isinstance(offload.probe_reachability(offload.ProcessController(), config), offload.UnreachableHost):
            continue
        claim = mac_test.CliArguments()
        claim.pid, claim.target, claim.repo_root = args.pid, target.name, args.repo_root
        claim.settings, claim.package, claim.verb = str(target.settings), args.package, verb
        with redirect_stdout(io.StringIO()):
            status = mac_test.command_claim_wait(claim, paths)
        if status not in (0, 13):
            if status == 11 and target.platform == "macos":
                busy_mac = True
            continue
        try:
            write_text_atomic(destination, f"{target.name}\t{target.settings}\n")
        except BaseException:
            _ = command_release_wait(args.pid)
            raise
        return 0
    return 11 if busy_mac else 1


def command_release_wait(pid: int) -> int:
    """Release only process-bound reservations; remote claims retain ownership."""
    start = process_identity.process_start(pid)
    if not isinstance(start, process_identity.Started):
        return 0
    for target in read_targets():
        if not isinstance(target, SshTarget):
            continue
        paths = mac_test.state_paths(target.name)
        if not paths.directory.exists():
            continue
        with mac_test.locked(paths.lock):
            for claim in mac_test.read_claims(paths):
                if (claim.record["pid"] == pid and claim.record["proc_start"] == start.start
                        and "remote_host" not in claim.record):
                    claim.path.unlink(missing_ok=True)
    return 0


def command_kind_eligible(repo_root: Path, package: str, kind: StepKind, routing: Routing) -> int:
    try:
        eligibility = tuple(
            ssh_target_eligibility(target, repo_root, package, kind, routing)
            if isinstance(target, SshTarget) else EligibleTarget(target)
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
    retry_allowed = kind in ("test", "check", "lint", "doc") and not isinstance(routing, LocalRequired)
    remote_results = tuple(result for result in eligibility if isinstance(result.target, SshTarget))
    disk_declined = any(isinstance(result, ExcludedTarget) and result.reason == "disk" for result in remote_results)
    linux_allowed = False
    if retry_allowed:
        if disk_declined:
            # Keep disk evidence independent of target order and retry permission.
            decision = NatedevOnly("disk")
        elif any(target_can_retry(result) for result in remote_results):
            decision = MacAllowed()
            linux_allowed = any(isinstance(result.target, SshTarget) and result.target.platform == "linux"
                                and target_can_retry(result) for result in remote_results)
        else:
            reasons = sorted(result.reason for result in remote_results if isinstance(result, ExcludedTarget))
            if reasons:
                decision = NatedevOnly(reasons[0])
    print("ssh_allowed" if linux_allowed else "mac_allowed" if isinstance(decision, MacAllowed)
          else f"natedev_only {display_field(decision.reason)}")
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


@dataclass(frozen=True)
class NoBlockingHold:
    """No hold in force prevents this session from starting."""


def hold_waited_on(holds: build_hold.NatedevHolds,
                   owner: build_hold.OwningSession | build_hold.NoOwningSession,
                   directory: Path) -> build_hold.Holder | NoBlockingHold:
    """Choose the oldest hold in force that the caller does not own."""
    own_key = ""
    if isinstance(owner, build_hold.OwningSession):
        try:
            own_key = build_hold.holder_path(directory, owner.session_id).name
        except ValueError:
            pass
    for holder in holds.in_force:
        if holder.key != own_key:
            return holder
    return NoBlockingHold()


def holds_only_own(owner: build_hold.OwningSession, directory: Path) -> bool:
    holds = build_hold.natedev_holds(directory)
    return bool(holds.in_force) and isinstance(hold_waited_on(holds, owner, directory), NoBlockingHold)


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
            blocking = hold_waited_on(holds, build_hold.owning_session(), directory)
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


def command_check_local(caller: str = "") -> int:
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
        owner = build_hold.owning_session()
        blocking = hold_waited_on(holds, owner, directory)
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
            if isinstance(owner, build_hold.OwningSession):
                _ = build_hold.register_wait(owner.session_id)
            print(f"natedev is held by {display_field(blocking.name)} for {display_field(blocking.purpose)}; launch after the release.")
        return 10
    except (ConfigError, OSError, build_hold.ReleaseRecordReadError) as error:
        print(wait_diagnostic(str(error), "" if caller == "verify" else caller), file=sys.stderr if caller else sys.stdout)
        return 12


def main() -> int:
    override_guard.refuse_overrides(Path(__file__), "build_targets/build_targets.py")
    args = parser().parse_args(namespace=CliArguments())
    if args.command == "reserve":
        return command_reserve(args)
    if args.command == "release-wait":
        return command_release_wait(args.pid)
    if args.command == "eligible":
        routing = LocalRequired(args.local_reason) if args.local_reason else ANY_ELIGIBLE_TARGET
        if args.target:
            return command_pinned_eligible(args, routing)
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
