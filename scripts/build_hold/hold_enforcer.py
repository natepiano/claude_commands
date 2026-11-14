#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Stop tests and builds outside a natedev hold, independent of their launch path."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_hold import build_hold
from scripts.build_hold.hold_applies import BUILDING_CARGO_VERBS, cargo_verb
from scripts.lib.suite_runners import SUITE_RUNNERS, starts_suite_runner

from scripts.direct import step_record
from scripts.buildlog import store
from scripts.lib.boot_identity import boot_identity
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.lib.process_group import send_process_group


def ci_placement_lines(proc_root: Path) -> list[str]:
    """Name live local CI placements from the existing admission records."""
    from scripts.build_queue import admission_service, admission_protocol as wire, placement
    from scripts.buildlog import store

    try:
        state = admission_service.read_state(store.root() / "admission")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        return [f"CI placements unreadable: {error}"]
    rows: dict[str, tuple[str, str, datetime, int]] = {}
    for episode, record in state.reservations.items():
        if not isinstance(record.place, wire.LocalPlace):
            continue
        if (build_hold.process_start_time(record.owner.pid, proc_root) == record.owner.start_time
                and build_hold.ci_process(record.owner.pid, record.owner.start_time, proc_root)):
            rows[episode] = ("running", record.queue_waiter.get("session_name") or record.step,
                             build_hold.aware_instant(record.admitted_at), record.owner.pid)
    for episode, request in state.placement_requests.items():
        if episode in rows or not build_hold.ci_process(request.owner.pid, request.owner.start_time, proc_root):
            continue
        saved = state.assignments.get(state.current_assignments.get(episode, ""))
        if saved is not None:
            if not isinstance(saved.state, placement.CurrentAssignment) or saved.assignment.destination.provider != "local":
                continue
            status, since = "running", saved.assignment.since
        else:
            if (not any(candidate.destination.provider == "local" for candidate in request.constraints.candidates)
                    or any(item.assignment.episode == episode and isinstance(item.state, placement.ClosedAssignment)
                           and item.state.reason in {"completed", "cancelled"} for item in state.assignments.values())):
                continue
            status, since = "queued", request.since
        rows[episode] = (status, request.identity.name or request.identity.step or request.profile.step,
                         datetime.fromtimestamp(since, timezone.utc), request.owner.pid)
    for place in state.places.values():
        if place.episode in rows or place.episode in state.placement_requests:
            continue
        if build_hold.ci_process(place.owner.pid, place.owner.start_time, proc_root):
            rows[place.episode] = ("queued", place.queue_identity["session_name"] or place.queue_identity["step"],
                datetime.fromtimestamp(place.since, timezone.utc), place.owner.pid)
    return [f"  CI {status}: {name} since {since.astimezone(user_zone()):%Y-%m-%d %H:%M:%S %Z} (pid {pid})"
            for status, name, since, pid in sorted(rows.values(), key=lambda row: (row[2], row[3]))]


def admitted_processes() -> frozenset[build_hold.ProcessIdentity]:
    """Read durable grants, which precede replies and the next decision pass."""
    from scripts.build_queue import admission_service

    try:
        state = admission_service.read_state(store.root() / "admission")
    except (OSError, ValueError) as error:
        raise OSError(f"admission state unavailable: {error}") from error
    return frozenset(build_hold.ProcessIdentity(record.owner.pid, record.owner.start_time)
                     for record in state.reservations.values())


@dataclass(frozen=True)
class WaitingRun:
    process: build_hold.AuditProcess
    owner: OwningSession | NoOwningSession


@dataclass(frozen=True)
class WorkingRun:
    process: build_hold.AuditProcess
    owner: OwningSession | NoOwningSession
    reason: str


@dataclass(frozen=True)
class StoppedRun:
    run: WaitingRun


@dataclass(frozen=True)
class RunIdentityChanged:
    run: WaitingRun


@dataclass(frozen=True)
class DetachedRun:
    supervisor: build_hold.AuditProcess
    path: Path


@dataclass(frozen=True)
class NoDetachedRun:
    pass


@dataclass(frozen=True)
class DetachedRunChanged:
    pass


@dataclass(frozen=True)
class NoStoppedRun:
    pass


NO_STOPPED_RUN = NoStoppedRun()


def claimed_processes(proc_root: Path = build_hold.PROC_ROOT) -> build_hold.LiveClaims | build_hold.ClaimsUnreadable:
    """Read local claim records without contacting the Mac or AWS."""
    from scripts.build_targets import target_config
    from scripts.mac_test import claim_state

    claimed: set[build_hold.ProcessIdentity] = set()
    try:
        for target in target_config.read_targets():
            if not isinstance(target, target_config.SshTarget):
                continue
            paths = claim_state.state_paths(target.name)
            for claim in claim_state.read_claims(paths):
                identity = build_hold.ProcessIdentity(claim.record["pid"], claim.record["proc_start"])
                if build_hold.process_start_time(identity.pid, proc_root) == identity.start_time:
                    claimed.add(identity)
    except claim_state.ClaimStateUnreadable as error:
        return build_hold.ClaimsUnreadable(f"{error.path}: {error}")
    except target_config.ConfigError as error:
        return build_hold.ClaimsUnreadable(str(error))
    return build_hold.LiveClaims(frozenset(claimed))


def holds_claim(
    process: build_hold.AuditProcess, processes: Mapping[int, build_hold.AuditProcess],
    claimed: frozenset[build_hold.ProcessIdentity],
) -> bool:
    """Find a live claim on this process, an ancestor, or a descendant."""
    if not claimed:
        return False
    if any(item.identity in claimed for item in (process, *build_hold.audit_ancestors(process, processes))):
        return True
    for identity in claimed:
        child = processes.get(identity.pid)
        if (child is not None and child.identity == identity
                and any(parent.identity == process.identity for parent in build_hold.audit_ancestors(child, processes))):
            return True
    return False


def is_outermost_gate(process: build_hold.AuditProcess, ancestors: Sequence[build_hold.AuditProcess]) -> bool:
    """A gate with no gate above it owns its whole process family."""
    return is_waiting_gate(process) and not any(is_waiting_gate(parent) for parent in ancestors)


def classify_drain_runs(
    processes: Mapping[int, build_hold.AuditProcess],
    admitted: frozenset[build_hold.ProcessIdentity],
    claimed: build_hold.LiveClaims | build_hold.ClaimsUnreadable,
) -> tuple[WaitingRun | WorkingRun, ...] | build_hold.ClaimsUnreadable:
    """Classify each gate by its descendants, reservations, and target claims."""
    if isinstance(claimed, build_hold.ClaimsUnreadable):
        return claimed
    runs: list[WaitingRun | WorkingRun] = []
    ancestors = {pid: build_hold.audit_ancestors(process, processes) for pid, process in processes.items()}
    for process in processes.values():
        if not is_outermost_gate(process, ancestors[process.identity.pid]):
            continue
        owner = build_hold.audit_owner(process, ancestors[process.identity.pid])
        family = [item for item in processes.values()
                  if item.identity == process.identity or process in ancestors[item.identity.pid]]
        identities = {item.identity for item in (*family, *ancestors[process.identity.pid])}
        reason = ("cargo child" if any(build_hold.audit_command(item) in build_hold.CARGO_COMMANDS
                                        for item in family) else
                  "admission reservation" if identities & admitted else
                  "Mac or AWS claim" if holds_claim(process, processes, claimed.identities) else "")
        if reason:
            runs.append(WorkingRun(process, owner, reason))
        else:
            runs.append(WaitingRun(process, owner))
    return tuple(runs)


def drain_identities(
    proc_root: Path, identity: build_hold.HolderIdentity,
) -> tuple[build_hold.ProcessIdentity, ...]:
    """Keep existing gates, CI workers, and builds that the hold could stop."""
    processes = build_hold.read_audit_processes(proc_root)
    holder = build_hold.Holder(identity.name, datetime.now(timezone.utc), "drain",
                               build_hold.UnknownReleaseEta(), identity.key)
    claims = claimed_processes(proc_root)
    claimed: frozenset[build_hold.ProcessIdentity] = (
        claims.identities if isinstance(claims, build_hold.LiveClaims) else frozenset())
    candidates = {decision.process.identity for decision in
                  decide_builds(processes, (holder,), os.getuid(), os.getpid(), claimed)
                  if isinstance(decision, WouldStopBuild)}
    candidates.update(process.identity for process in processes.values()
                      if is_waiting_gate(process) or process.command == "Runner.Worker"
                      or process.executable == "Runner.Worker"
                      or process.argv and Path(process.argv[0]).name == "Runner.Worker")
    return tuple(process.identity for process in processes.values()
                 if process.identity in candidates and not any(
                     parent.identity in candidates for parent in build_hold.audit_ancestors(process, processes)))


def wait_for_kept_runs(
    holder: build_hold.Holder, proc_root: Path = build_hold.PROC_ROOT, *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> int:
    """Wait for the kept identities without stopping any process."""
    heartbeat = clock()
    try:
        while remaining := build_hold.live_kept_runs(holder, proc_root):
            if clock() >= heartbeat:
                print(f"draining: {len(remaining)} runs remain", flush=True)
                heartbeat = clock() + 60
            sleep(1)
    except KeyboardInterrupt:
        print("drain interrupted; the hold remains active", flush=True)
        return 130
    print("drained; natedev stays held until release", flush=True)
    return 0


def working_drain_identities(proc_root: Path) -> tuple[build_hold.ProcessIdentity, ...]:
    """Keep existing work through the enforcer when the drain hold starts."""
    processes = build_hold.read_audit_processes(proc_root)
    claims = claimed_processes(proc_root)
    runs = classify_drain_runs(processes, admitted_processes(), claims)
    if isinstance(runs, build_hold.ClaimsUnreadable):
        print(f"claims unreadable: {runs.reason}", file=sys.stderr)
        return tuple(process.identity for process in processes.values()
                     if is_outermost_gate(process, build_hold.audit_ancestors(process, processes)))
    return tuple(run.process.identity for run in runs if isinstance(run, WorkingRun))


def detached_run(
    process: build_hold.AuditProcess, processes: Mapping[int, build_hold.AuditProcess],
) -> DetachedRun | NoDetachedRun | DetachedRunChanged:
    """Find the supervisor by ancestry or the command's recorded environment."""
    lineage = (process, *build_hold.audit_ancestors(process, processes))
    for parent in lineage:
        for index, argument in enumerate(parent.argv[:-2]):
            if Path(argument).name == "detached_runs.py" and parent.argv[index + 1] == "supervise":
                path = Path(parent.argv[index + 2])
                return DetachedRun(parent, path if path.is_absolute() else Path(parent.cwd) / path)
    record_path = next((item.environment["UNIT_DETACHED_RECORD"] for item in lineage
                        if item.environment.get("UNIT_DETACHED_RECORD")), "")
    candidates = [Path(record_path)] if record_path else sorted({
        path for item in lineage if item.environment.get("UNIT_RUN_DIR")
        for path in (Path(item.environment["UNIT_RUN_DIR"]) / "detached_runs").glob("*.json")
    })
    for path in candidates:
        try:
            raw = cast(object, json.loads(path.read_text()))
        except FileNotFoundError:
            continue
        if not isinstance(raw, dict):
            continue
        record = cast(dict[str, object], raw)
        pid = record.get("pid")
        if isinstance(pid, int) and pid in processes and (record_path or any(item.identity.pid == pid for item in lineage)):
            return DetachedRun(processes[pid], path)
    return DetachedRunChanged() if record_path else NoDetachedRun()


def supervisor_args(pid: int, proc_root: Path) -> tuple[str, ...]:
    try:
        return tuple(part.decode("utf-8", "replace") for part in
                     (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0") if part)
    except OSError:
        return ()


def supervisor_matches(argv: tuple[str, ...], path: Path, cwd: str) -> bool:
    """Match the supervisor script, verb, and record argument in their argv positions."""
    if len(argv) != 4 or Path(argv[1]).name != "detached_runs.py" or argv[2] != "supervise":
        return False
    argument = Path(argv[3])
    record_path = argument if argument.is_absolute() else Path(cwd) / argument
    return record_path.resolve() == path.resolve()


def stop_waiting_run(
    run: WaitingRun, holder: build_hold.Holder, processes: Mapping[int, build_hold.AuditProcess], proc_root: Path,
    *, signal_process: Callable[[int, int], None] = os.kill,
    signal_group: Callable[[int, int], None] = send_process_group,
    supervisor_args: Callable[[int, Path], tuple[str, ...]] = supervisor_args,
) -> StoppedRun | RunIdentityChanged:
    """Check process identities before TERM and record each stopped gate."""
    from scripts.agents import detached_runs

    process = run.process
    identity = process.identity
    holders = (holder, *build_hold.natedev_holds(build_hold.holder_directory()).in_force)
    if isinstance(keeping_holder(process, build_hold.audit_ancestors(process, processes), processes, holders), build_hold.Holder):
        return RunIdentityChanged(run)
    if build_hold.process_start_time(identity.pid, proc_root) != identity.start_time:
        return RunIdentityChanged(run)
    detached = detached_run(process, processes)
    if isinstance(detached, DetachedRunChanged):
        return RunIdentityChanged(run)
    if isinstance(detached, DetachedRun):
        supervisor = detached.supervisor.identity
        if isinstance(keeping_holder(detached.supervisor, build_hold.audit_ancestors(detached.supervisor, processes),
                                     processes, holders), build_hold.Holder):
            return RunIdentityChanged(run)
        if (build_hold.process_start_time(supervisor.pid, proc_root) != supervisor.start_time
                or not supervisor_matches(supervisor_args(supervisor.pid, proc_root), detached.path, detached.supervisor.cwd)):
            return RunIdentityChanged(run)
    classification: build_hold.BuildClass = "other_session" if isinstance(run.owner, OwningSession) else "no_session"
    record_build(build_hold.AuditedBuild(process, run.owner, classification, ()), holder, proc_root, "stopped",
                 stopped_run=identity)
    reason = f"stopped for {holder.purpose}; run again after the release"

    def still_current() -> bool:
        current = build_hold.process_start_time(identity.pid, proc_root) == identity.start_time
        if isinstance(detached, DetachedRun):
            supervisor = detached.supervisor.identity
            current = current and build_hold.process_start_time(supervisor.pid, proc_root) == supervisor.start_time
        return current

    def signal_run() -> None:
        if isinstance(detached, DetachedRun):
            signal_group(detached.supervisor.identity.pid, signal.SIGTERM)
        else:
            signal_process(identity.pid, signal.SIGTERM)

    try:
        if isinstance(run.owner, OwningSession):
            _ = build_hold.register_wait(run.owner.session_id)
        if isinstance(detached, DetachedRun):
            sent = detached_runs.stop_with_reason(detached.path, reason, still_current=still_current, signal_run=signal_run)
        else:
            sent = still_current()
            if sent:
                signal_run()
        if not sent:
            set_stop_status(holder, identity, "not_stopped")
            return RunIdentityChanged(run)
    except ProcessLookupError:
        set_stop_status(holder, identity, "not_stopped")
        return RunIdentityChanged(run)
    except BaseException:
        set_stop_status(holder, identity, "not_stopped")
        raise
    set_stop_status(holder, identity, "stopped")
    if isinstance(detached, NoDetachedRun):
        for item in processes.values():
            if (process in build_hold.audit_ancestors(item, processes)
                    and not isinstance(keeping_holder(item, build_hold.audit_ancestors(item, processes),
                                                      processes, holders), build_hold.Holder)
                    and build_hold.process_start_time(item.identity.pid, proc_root) == item.identity.start_time):
                try:
                    signal_process(item.identity.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    return StoppedRun(run)


def clear_queued_runs(proc_root: Path) -> None:
    """Stop existing gates outside the hold's allow rules."""
    try:
        processes = read_processes(proc_root, os.getuid())
        claims = claimed_processes(proc_root)
    except (OSError, ValueError) as error:
        print(f"Clear unavailable: {error}", file=sys.stderr)
        return
    if isinstance(claims, build_hold.ClaimsUnreadable):
        print(f"claims unreadable; clear waits: {claims.reason}", file=sys.stderr)
        return
    holders = build_hold.natedev_holds(build_hold.holder_directory()).in_force
    stop_queued_runs(processes, holders, os.getuid(), os.getpid(), claims.identities, proc_root)


def stop_queued_runs(
    processes: Mapping[int, build_hold.AuditProcess], holders: Sequence[build_hold.Holder],
    uid: int, enforcer_pid: int, claimed: frozenset[build_hold.ProcessIdentity], proc_root: Path, *,
    before: Mapping[str, int] | None = None,
    stopped: set[build_hold.ProcessIdentity] | None = None,
    signal_process: Callable[[int, int], None] = os.kill,
) -> None:
    """Apply the clear rule after checking claims and the current holder set."""
    for decision in decide_builds(processes, holders, uid, enforcer_pid, claimed, drain=True):
        if (not isinstance(decision, WouldStopBuild)
                or not is_outermost_gate(decision.process, build_hold.audit_ancestors(decision.process, processes))):
            continue
        identity = decision.process.identity
        if stopped is not None and identity in stopped:
            continue
        if before is not None and int(identity.start_time) >= before[decision.holder.key]:
            continue
        current = build_hold.natedev_holds(build_hold.holder_directory()).in_force
        if current != tuple(holders):
            return
        result = stop_waiting_run(WaitingRun(decision.process, decision.owner), decision.holder, processes, proc_root,
                                  signal_process=signal_process)
        if isinstance(result, StoppedRun) and stopped is not None:
            stopped.add(identity)
        label = "stopped" if isinstance(result, StoppedRun) else "skipped"
        print(f"{label}: pid {identity.pid}", flush=True)


def drain(
    holder: build_hold.Holder, proc_root: Path = build_hold.PROC_ROOT, *,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
) -> int:
    """Stop waiting gates and wait for working gates while the hold stays active."""
    attempted: set[build_hold.ProcessIdentity] = set()
    signalled: set[build_hold.ProcessIdentity] = set()
    heartbeat = clock()
    claims_unreadable = False
    try:
        while True:
            processes = build_hold.read_audit_processes(proc_root)
            claims = claimed_processes(proc_root)
            runs = classify_drain_runs(processes, admitted_processes(), claims)
            if isinstance(runs, build_hold.ClaimsUnreadable):
                if not claims_unreadable:
                    print(f"claims unreadable: {runs.reason}", file=sys.stderr)
                claims_unreadable = True
                sleep(1)
                continue
            claims_unreadable = False
            if not runs:
                print("drained", flush=True)
                return 0
            report = clock() >= heartbeat
            for run in runs:
                owner = run.owner.session_id if isinstance(run.owner, OwningSession) else "no session"
                if isinstance(run, WaitingRun) and run.process.identity not in attempted:
                    outcome = stop_waiting_run(run, holder, processes, proc_root)
                    label = "stopped" if isinstance(outcome, StoppedRun) else "skipped: identity changed"
                    print(f"{label}: {owner}, pid {run.process.identity.pid}", flush=True)
                    attempted.add(run.process.identity)
                    if isinstance(outcome, StoppedRun):
                        signalled.add(run.process.identity)
                if report:
                    try:
                        uptime = float((proc_root / "uptime").read_text().split()[0])
                    except (OSError, ValueError, IndexError):
                        uptime = clock()
                    age = max(0, uptime - int(run.process.identity.start_time) / os.sysconf("SC_CLK_TCK"))
                    if isinstance(run, WorkingRun):
                        state = "working"
                    elif run.process.identity in signalled:
                        state = "TERM sent; still alive"
                    else:
                        state = "skipped: identity changed"
                    detail = f": {run.reason}" if isinstance(run, WorkingRun) else ""
                    print(f"{state}: {owner}, pid {run.process.identity.pid}, age {age:.0f}s{detail}", flush=True)
            if report:
                print(f"draining: {len(runs)} runs remain", flush=True)
                heartbeat = clock() + 60
            sleep(1)
    except KeyboardInterrupt:
        print("drain interrupted; the hold remains active", flush=True)
        return 130


@dataclass(frozen=True)
class AllowedBuild:
    process: build_hold.AuditProcess
    owner: OwningSession | NoOwningSession
    reason: str


@dataclass(frozen=True)
class KeptBuild:
    process: build_hold.AuditProcess
    owner: OwningSession | NoOwningSession
    holder: build_hold.Holder

    @property
    def reason(self) -> str:
        return f"kept by {self.holder.name}, with PID and start time checked"


@dataclass(frozen=True)
class WouldStopBuild:
    process: build_hold.AuditProcess
    owner: OwningSession | NoOwningSession
    reason: str
    holder: build_hold.Holder
    stop_root: build_hold.AuditProcess


BuildDecision = AllowedBuild | KeptBuild | WouldStopBuild


@dataclass(frozen=True)
class NotKept:
    """No holder keeps this process, its family, or its step controller."""


def keeping_holder(
    process: build_hold.AuditProcess, ancestors: Sequence[build_hold.AuditProcess],
    processes: Mapping[int, build_hold.AuditProcess], holders: Sequence[build_hold.Holder],
) -> build_hold.Holder | NotKept:
    """Protect kept processes, their ancestors, their descendants, and their step controllers."""
    lineage = (process, *ancestors)
    for item in (process, *ancestors):
        call_id = item.environment.get("BUILDLOG_CALL_ID", "")
        if not call_id:
            continue
        record = step_record.read(call_id)
        if isinstance(record, step_record.StepRecord):
            controller = processes.get(record.pid)
            if (controller is not None and controller.uid == process.uid
                    and controller.identity == build_hold.ProcessIdentity(record.pid, record.proc_start)):
                lineage += (controller, *build_hold.audit_ancestors(controller, processes))
    for holder in holders:
        if any(item.identity in holder.keep for item in lineage):
            return holder
        for kept in holder.keep:
            child = processes.get(kept.pid)
            if (child is not None and child.identity == kept
                    and process in build_hold.audit_ancestors(child, processes)):
                return holder
    return NotKept()


def state_directory() -> Path:
    return Path(os.environ.get("BUILD_HOLD_ENFORCER_DIR", str(Path.home() / ".local/state/build-hold-enforcer")))


def clear_cutoff_path(key: str, since: datetime) -> Path:
    hold_id = hashlib.sha256(json.dumps([key, since.isoformat()]).encode()).hexdigest()
    return state_directory() / "clear-cutoffs" / f"{hold_id}.json"


def record_clear_cutoff(identity: build_hold.HolderIdentity, since: datetime) -> None:
    """Save the boot-relative cutoff before publishing the clear hold."""
    before = time.clock_gettime_ns(time.CLOCK_BOOTTIME) * os.sysconf("SC_CLK_TCK") // 1_000_000_000
    path = clear_cutoff_path(identity.key, since)
    write_text_atomic(path, json.dumps({"boot_id": boot_identity(), "before": before}) + "\n")


def read_clear_cutoff(holder: build_hold.Holder) -> int:
    """Leave gates waiting if the cutoff is missing, invalid, or from another boot."""
    try:
        raw = cast(object, json.loads(clear_cutoff_path(holder.key, holder.since).read_text()))
        if isinstance(raw, dict):
            record = cast(dict[str, object], raw)
            before = record.get("before")
            if (record.get("boot_id") == boot_identity()
                    and type(before) is int and before >= 0):
                return before
    except (OSError, ValueError):
        pass
    return 0


def read_processes(
    proc_root: Path, uid: int, cgroup_root: Path = build_hold.SYSTEM_CGROUP_ROOT,
) -> dict[int, build_hold.AuditProcess]:
    """Include call-id processes even when their names are not build commands."""
    processes = build_hold.read_audit_processes(proc_root, build_hold.HoldBuildCandidates(uid), cgroup_root)
    for pid, process in tuple(processes.items()):
        if process.uid != uid or process.argv:
            continue
        directory = proc_root / str(pid)
        try:
            environment = dict(part.decode("utf-8", "replace").split("=", 1) for part in
                               (directory / "environ").read_bytes().split(b"\0") if b"=" in part)
        except OSError:
            continue
        process = replace(process, environment=environment)
        if environment.get("BUILDLOG_CALL_ID"):
            try:
                argv = tuple(part.decode("utf-8", "replace") for part in
                             (directory / "cmdline").read_bytes().split(b"\0") if part)
                cwd = str((directory / "cwd").readlink())
            except OSError:
                argv, cwd = (), ""
            process = replace(process, argv=argv, cwd=cwd)
        processes[pid] = process
    return processes


def python_target(arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Read the Python entry point, not arguments passed to another script."""
    skip_value = False
    for index, argument in enumerate(arguments):
        if skip_value:
            skip_value = False
        elif argument in {"-W", "-X", "--check-hash-based-pycs"}:
            skip_value = True
        elif argument == "--":
            return arguments[index + 1:]
        elif argument in {"-m", "-c"} or not argument.startswith("-"):
            return arguments[index:]
        elif argument.startswith("-m"):
            return ("-m", argument[2:], *arguments[index + 1:])
    return ()


def is_waiting_gate(process: build_hold.AuditProcess) -> bool:
    # These scripts wait themselves. A quoted mention in a shell command is not a script path.
    if not process.argv:
        return False
    executable = Path(process.argv[0]).name
    if executable == "verify.sh" or starts_suite_runner(process.argv):
        return True
    if executable.startswith("python"):
        target = python_target(process.argv[1:])
        return starts_suite_runner(target)
    if executable not in {"bash", "sh", "dash", "zsh"}:
        return False
    for argument in process.argv[1:]:
        if argument in {"-c", "-lc", "-ic"}:
            return False
        if not argument.startswith("-"):
            return Path(argument).name == "verify.sh"
    return False


def is_build(process: build_hold.AuditProcess) -> bool:
    names = {process.executable, Path(process.argv[0]).name if process.argv else process.command}
    if "cargo" in names:
        return cargo_verb(process.argv[1:]) in BUILDING_CARGO_VERBS
    if "rustc" in names:
        arguments = process.argv[1:]
        if arguments and arguments[0].startswith("+"):
            arguments = arguments[1:]
        if arguments in {("-vV",), ("-V",), ("--version",)}:
            return False
    if names & ((build_hold.AUDIT_COMMANDS - {"cargo"}) | {"clippy-driver", "pytest", "py.test", "basedpyright"} | SUITE_RUNNERS):
        return True
    if "rust-analyzer" in names:
        # Ownership needs ancestors, so it is checked by decide_builds.
        return True
    if any(name.startswith("python") for name in names):
        if any(Path(name).match("test_*.py") for name in names):
            return True
        target = python_target(process.argv[1:])
        return (target[:2] in {("-m", "unittest"), ("-m", "pytest")}
                or starts_suite_runner(target)
                or bool(target) and (Path(target[0]).name in {"pytest", "py.test", "basedpyright"}
                                     or Path(target[0]).match("test_*.py")))
    if names & {"node", "nodejs"}:
        return any(Path(argument).name in {"basedpyright", "basedpyright.js"} for argument in process.argv[1:])
    if names & {"bash", "sh", "dash", "zsh"}:
        if any(Path(name).match("test_*.sh") for name in names):
            return True
        for argument in process.argv[1:]:
            if argument in {"-c", "-lc", "-ic"}:
                return False
            if not argument.startswith("-"):
                return Path(argument).match("test_*.sh")
    return False


def is_build_like(process: build_hold.AuditProcess) -> bool:
    return bool(process.environment.get("BUILDLOG_CALL_ID")) or is_build(process) or is_waiting_gate(process)


def is_sccache_server(process: build_hold.AuditProcess) -> bool:
    names = {process.executable, Path(process.argv[0]).name if process.argv else process.command}
    return "sccache" in names and (
        any(argument in {"--start-server", "--server", "server"} for argument in process.argv[1:])
        or process.environment.get("SCCACHE_START_SERVER", "") in {"1", "true"}
        or process.environment.get("SCCACHE_SERVER", "") in {"1", "true"})


def protected_reason(
    process: build_hold.AuditProcess, ancestors: Sequence[build_hold.AuditProcess],
    owner: OwningSession | NoOwningSession,
    holders: Sequence[build_hold.Holder], enforcer_ancestors: frozenset[build_hold.ProcessIdentity],
    *, drain: bool = False,
) -> str:
    if process.identity in enforcer_ancestors:
        return "enforcer or its ancestor"
    if build_hold.owner_is_sole_holder(owner, holders):
        return "owned by the sole in-force holder"
    call_id = process.environment.get("BUILDLOG_CALL_ID", "")
    if call_id:
        record = step_record.read(call_id)
        if isinstance(record, step_record.StepRecord):
            if process.identity == build_hold.ProcessIdentity(record.pid, record.proc_start):
                return f"step {call_id} controller stays alive to route the step"
            if record.state != step_record.StepState.ON_NATEDEV:
                return f"step {call_id} is {record.state.value}"
        # Missing, stale and unreadable records grant no permission.
        return ""
    if is_waiting_gate(process):
        return "" if drain else "verify.sh or a suite runner waits at its own hold gate"
    if any(is_sccache_server(item) for item in (process, *ancestors)):
        return "sccache server compiles for the holder too"
    lineage = (process, *ancestors)
    markers = {"CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR",
               "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_SESSION_DIR"}
    if (not any(build_hold.audit_command(item) in {"claude", "codex"} for item in lineage)
            and not any(item.environment.get(marker) for item in lineage for marker in markers)
            and any(item.tty_nr != 0 or item.desktop_app for item in lineage)):
        return "user terminal or desktop app, with no agent ancestor or session marker"
    return ""


def gate_job_root(
    process: build_hold.AuditProcess, ancestors: Sequence[build_hold.AuditProcess],
) -> build_hold.AuditProcess:
    """Stop the waiting gate's job while the gate stays alive."""
    for child, parent in zip((process, *ancestors), ancestors):
        if is_waiting_gate(parent):
            return child
    return process


def decide_builds(
    processes: Mapping[int, build_hold.AuditProcess], holders: Sequence[build_hold.Holder],
    uid: int, enforcer_pid: int, claimed: frozenset[build_hold.ProcessIdentity],
    *, drain: bool = False,
) -> tuple[BuildDecision, ...]:
    protected: frozenset[build_hold.ProcessIdentity] = frozenset()
    if enforcer_pid in processes:
        enforcer = processes[enforcer_pid]
        protected = frozenset(item.identity for item in (enforcer, *build_hold.audit_ancestors(enforcer, processes)))
    decisions: list[BuildDecision] = []
    for process in sorted(processes.values(), key=lambda item: (len(build_hold.audit_ancestors(item, processes)), item.identity.pid)):
        if process.uid != uid or not is_build_like(process):
            continue
        ancestors = build_hold.audit_ancestors(process, processes)
        owner = build_hold.audit_owner(process, ancestors)
        names = {process.executable, Path(process.argv[0]).name if process.argv else process.command}
        if (not process.environment.get("BUILDLOG_CALL_ID") and "rust-analyzer" in names
                and isinstance(owner, NoOwningSession)):
            continue
        reason = ("holds a live Mac or AWS claim" if holds_claim(process, processes, claimed)
                  else protected_reason(process, ancestors, owner, holders, protected, drain=drain))
        if reason or not holders:
            decisions.append(AllowedBuild(process, owner, reason or "no hold is in force"))
            continue
        kept = keeping_holder(process, ancestors, processes, holders)
        if isinstance(kept, build_hold.Holder):
            decisions.append(KeptBuild(process, owner, kept))
            continue
        owner_key = build_hold.holder_path(Path(), build_hold.holder_of(owner.session_id).key).name if isinstance(owner, OwningSession) else ""
        blocking = [holder for holder in holders if holder.key != owner_key] or list(holders)
        holder = min(blocking, key=lambda item: item.since)
        decisions.append(WouldStopBuild(process, owner, f"test or build outside {holder.name}'s hold for {holder.purpose}",
                                       holder, gate_job_root(process, ancestors)))
    return tuple(decisions)


def hold_build_log(holder: build_hold.Holder) -> Path:
    hold_id = hashlib.sha256(json.dumps([holder.key, holder.since.isoformat()]).encode()).hexdigest()
    return store.root() / store.host_name() / f"hold-builds-{hold_id}.jsonl"


def set_stop_status(holder: build_hold.Holder, identity: build_hold.ProcessIdentity,
                    status: Literal["stopped", "not_stopped"]) -> None:
    """Complete or cancel the durable stop request under its existing log lock."""
    path = hold_build_log(holder)
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines: list[str] = []
        for line in path.read_text().splitlines():
            entry = cast(dict[str, object], json.loads(line))
            if (entry.get("pid") == identity.pid and entry.get("proc_start") == identity.start_time
                    and entry.get("action") == "stopped" and "stopped_run" in entry):
                entry["stop_status"] = status
                entry["action"] = status
            lines.append(json.dumps(entry))
        write_text_atomic(path, "\n".join(lines) + "\n")


def record_build(
    build: build_hold.AuditedBuild, holder: build_hold.Holder, proc_root: Path,
    action: Literal["audit", "stopped"],
    *, stopped_run: build_hold.ProcessIdentity | NoStoppedRun = NO_STOPPED_RUN,
) -> None:
    """Audit and enforcement use the same permanent log and append writer."""
    try:
        boot = boot_identity(proc_root)
    except (OSError, ValueError):
        boot = ""
    identity = build.process.identity
    build_id = hashlib.sha256(json.dumps([boot, str(identity.pid), identity.start_time]).encode()).hexdigest()
    host = store.host_name()
    path = hold_build_log(holder)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep duplicate reads and the shared append in one lock, without nesting the append writer's file lock.
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            value = cast(object, json.loads(line))
            if isinstance(value, dict):
                row = cast(dict[str, object], value)
                if row.get("id") == build_id and row.get("action", "audit") == action:
                    return
        entry: dict[str, object] = {
            "kind": "hold_build", "v": 1, "id": build_id, "at": store.utc_iso(time.time()), "host": host,
            "hold_key": holder.key, "hold_since": holder.since.isoformat(), "boot_id": boot,
            "pid": identity.pid, "proc_start": identity.start_time, "class": build.classification,
            "session": build.owner.session_id if isinstance(build.owner, OwningSession) else "",
            "command": shlex.join(build.process.argv) or build.process.command, "cwd": build.process.cwd,
            "action": action, "call_id": build.process.environment.get("BUILDLOG_CALL_ID", ""),
        }
        if isinstance(stopped_run, build_hold.ProcessIdentity):
            entry["stopped_run"] = {"pid": stopped_run.pid, "proc_start": stopped_run.start_time}
            entry["stop_status"] = "requested"
        store.append_line(path, entry)


def refusal_line(build: WouldStopBuild) -> str:
    return (f"natedev builds and tests are held by {build.holder.name} for {build.holder.purpose}; "
            "this run was stopped. You get a notice at the release; run it again then.\n")


def write_refusal(proc_root: Path, build: WouldStopBuild) -> None:
    """Best effort only; never wait on a full stderr pipe."""
    try:
        descriptor = os.open(proc_root / str(build.process.identity.pid) / "fd/2", os.O_WRONLY | os.O_NONBLOCK | os.O_APPEND)
        try:
            _ = os.write(descriptor, refusal_line(build).encode())
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        pass


class StopBuildsAndTests:
    def __init__(
        self, proc_root: Path, *, signal_process: Callable[[int, int], None] = os.kill,
        clock: Callable[[], float] = time.monotonic,
        record: Callable[[build_hold.AuditedBuild, build_hold.Holder, Path, Literal["audit", "stopped"]], None] = record_build,
        register: Callable[[str], str] = build_hold.register_wait,
    ) -> None:
        self.proc_root: Path = proc_root
        self.signal_process: Callable[[int, int], None] = signal_process
        self.clock: Callable[[], float] = clock
        self.record: Callable[[build_hold.AuditedBuild, build_hold.Holder, Path, Literal["audit", "stopped"]], None] = record
        self.terminating: dict[build_hold.ProcessIdentity, float] = {}
        self.register: Callable[[str], str] = register

    def signal_if_current(self, identity: build_hold.ProcessIdentity, signum: int) -> bool:
        if build_hold.process_start_time(identity.pid, self.proc_root) != identity.start_time:
            return False
        try:
            self.signal_process(identity.pid, signum)
            return True
        except ProcessLookupError:
            return False

    def kill_survivors(self, processes: Mapping[int, build_hold.AuditProcess],
                       holders: Sequence[build_hold.Holder], enforcer_pid: int,
                       claimed: frozenset[build_hold.ProcessIdentity]) -> None:
        if not self.terminating:
            return
        now = self.clock()
        enforcer = processes.get(enforcer_pid)
        protected: set[build_hold.ProcessIdentity] = {item.identity for item in (enforcer, *build_hold.audit_ancestors(enforcer, processes))} if enforcer else set()
        for identity, deadline in tuple(self.terminating.items()):
            process = processes.get(identity.pid)
            ancestors = build_hold.audit_ancestors(process, processes) if process is not None else ()
            owner = build_hold.audit_owner(process, ancestors) if process is not None else NoOwningSession()
            # A route change cannot cancel termination of an old process.
            if (not holders or identity in protected or build_hold.owner_is_sole_holder(owner, holders)
                    or process is not None and holds_claim(process, processes, claimed)
                    or process is not None and isinstance(
                        keeping_holder(process, ancestors, processes, holders), build_hold.Holder)):
                del self.terminating[identity]
                continue
            if now >= deadline:
                _ = self.signal_if_current(identity, signal.SIGKILL)
                del self.terminating[identity]

    def stop(self, build: WouldStopBuild, processes: Mapping[int, build_hold.AuditProcess],
             holders: Sequence[build_hold.Holder], enforcer_pid: int,
             claimed: frozenset[build_hold.ProcessIdentity]) -> None:
        root = build.stop_root
        if (root.identity in self.terminating or holds_claim(root, processes, claimed)
                or isinstance(keeping_holder(root, build_hold.audit_ancestors(root, processes), processes, holders), build_hold.Holder)):
            return
        ancestors = build_hold.audit_ancestors(build.process, processes)
        if isinstance(keeping_holder(build.process, ancestors, processes, holders), build_hold.Holder):
            return
        # An ancestor can exit or a PID can be reused after the table read.
        if any(build_hold.process_start_time(item.identity.pid, self.proc_root) != item.identity.start_time
               for item in (build.process, *ancestors)):
            return
        write_refusal(self.proc_root, build)
        classification: build_hold.BuildClass = "other_session" if isinstance(build.owner, OwningSession) else "no_session"
        self.record(build_hold.AuditedBuild(build.process, build.owner, classification, ()), build.holder, self.proc_root, "stopped")
        if isinstance(build.owner, OwningSession):
            _ = self.register(build.owner.session_id)
        # Include all descendants, not only compilers. Parents precede their children.
        children: dict[int, list[build_hold.AuditProcess]] = {}
        for process in processes.values():
            children.setdefault(process.parent_pid, []).append(process)
        protected: frozenset[build_hold.ProcessIdentity] = frozenset()
        if enforcer_pid in processes:
            enforcer = processes[enforcer_pid]
            protected = frozenset(item.identity for item in (enforcer, *build_hold.audit_ancestors(enforcer, processes)))
        pending = [root]
        seen: set[build_hold.ProcessIdentity] = set()
        while pending:
            process = pending.pop(0)
            if process.identity in seen or process.uid != root.uid:
                continue
            seen.add(process.identity)
            parents = build_hold.audit_ancestors(process, processes)
            owner = build_hold.audit_owner(process, parents)
            if holds_claim(process, processes, claimed):
                continue
            if protected_reason(process, parents, owner, holders, protected):
                continue
            if isinstance(keeping_holder(process, parents, processes, holders), build_hold.Holder):
                continue
            if process.identity not in self.terminating and self.signal_if_current(process.identity, signal.SIGTERM):
                self.terminating[process.identity] = self.clock() + 2
            pending.extend(child for child in children.get(process.identity.pid, [])
                           if int(child.identity.start_time) >= int(process.identity.start_time))


def start() -> None:
    """Keep an active unit; otherwise start the service or one detached fallback."""
    script = Path(__file__).resolve()
    python = str(script.parents[1] / "lib/py")
    if shutil.which("systemctl"):
        try:
            active = subprocess.run(["systemctl", "--user", "is-active", "build-hold-enforcer"],
                                    capture_output=True, text=True, timeout=5)
            if active.returncode == 0 or active.stdout.strip() == "activating":
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    bus = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")) or (Path(os.environ.get("XDG_RUNTIME_DIR", "/nonexistent")) / "bus").exists()
    if bus and shutil.which("systemd-run"):
        try:
            launched = subprocess.run([
                "systemd-run", "--user", "--unit=build-hold-enforcer", "--collect", "--", python, str(script), "run",
            ], capture_output=True, text=True, timeout=5)
            if launched.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    _ = subprocess.Popen([python, str(script), "run"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def run(
    proc_root: Path, holder_directory: Path, directory: Path, *,
    uid: int, enforcer_pid: int,
    stopper: StopBuildsAndTests, sleep: Callable[[float], None] = time.sleep,
    cgroup_root: Path = build_hold.SYSTEM_CGROUP_ROOT,
) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        claims_unreadable = False
        cleared: set[build_hold.ProcessIdentity] = set()
        while True:
            holders = build_hold.natedev_holds(holder_directory).in_force
            if not holders:
                return 0
            processes = read_processes(proc_root, uid, cgroup_root)
            claims = claimed_processes(proc_root)
            if isinstance(claims, build_hold.ClaimsUnreadable):
                if not claims_unreadable:
                    print(f"claims unreadable; enforcement waits: {claims.reason}", file=sys.stderr)
                claims_unreadable = True
                sleep(1)
                continue
            if claims_unreadable:
                print("claims readable; enforcement resumes", file=sys.stderr)
            claims_unreadable = False
            claimed = claims.identities
            before = {holder.key: read_clear_cutoff(holder) for holder in holders}
            stop_queued_runs(processes, holders, uid, enforcer_pid, claimed, proc_root,
                             before=before, stopped=cleared, signal_process=stopper.signal_process)
            stopper.kill_survivors(processes, holders, enforcer_pid, claimed)
            for decision in decide_builds(processes, holders, uid, enforcer_pid, claimed):
                if isinstance(decision, WouldStopBuild):
                    # Recheck the holder set in case a release or another hold followed the table read.
                    current_holders = build_hold.natedev_holds(holder_directory).in_force
                    if not current_holders:
                        return 0
                    current = decision if current_holders == holders else next(
                        item for item in decide_builds(processes, current_holders, uid, enforcer_pid, claimed)
                        if item.process.identity == decision.process.identity)
                    if isinstance(current, WouldStopBuild):
                        stopper.stop(current, processes, current_holders, enforcer_pid, claimed)
            sleep(1)


def decision_lines(decisions: Sequence[BuildDecision]) -> list[str]:
    lines: list[str] = []
    for decision in decisions:
        label = "would stop" if isinstance(decision, WouldStopBuild) else "kept" if isinstance(decision, KeptBuild) else "allowed"
        owner = decision.owner.session_id if isinstance(decision.owner, OwningSession) else "no session"
        command = shlex.join(decision.process.argv) or decision.process.command
        keep = f"; --keep {decision.process.identity.pid}" if isinstance(decision, WouldStopBuild) else ""
        lines.append(f"{label}: {owner}, pid {decision.process.identity.pid}: {command}; {decision.reason}{keep}")
    return lines


def report(proc_root: Path, directory: Path, *, drain: bool = False) -> list[str]:
    holders = build_hold.natedev_holds(directory).in_force
    lines = [f"kept by {holder.name}: pid {run.pid}, start {run.start_time}"
             for holder in holders for run in holder.keep]
    processes = read_processes(proc_root, os.getuid())
    claims = claimed_processes(proc_root) if holders else build_hold.LiveClaims(frozenset())
    if isinstance(claims, build_hold.ClaimsUnreadable):
        lines.append(f"claims unreadable: {claims.reason}")
    claimed: frozenset[build_hold.ProcessIdentity] = claims.identities if isinstance(claims, build_hold.LiveClaims) else frozenset()
    return lines + decision_lines(decide_builds(processes, holders, os.getuid(), os.getpid(), claimed, drain=drain))


def scan(proc_root: Path, holder_directory: Path, uid: int, enforcer_pid: int,
         cgroup_root: Path = build_hold.SYSTEM_CGROUP_ROOT) -> None:
    began = time.monotonic()
    holders = build_hold.natedev_holds(holder_directory).in_force
    processes = read_processes(proc_root, uid, cgroup_root)
    claims = claimed_processes(proc_root) if holders else build_hold.LiveClaims(frozenset())
    if isinstance(claims, build_hold.ClaimsUnreadable):
        print(f"claims unreadable: {claims.reason}")
    claimed: frozenset[build_hold.ProcessIdentity] = claims.identities if isinstance(claims, build_hold.LiveClaims) else frozenset()
    for line in decision_lines(decide_builds(processes, holders, uid, enforcer_pid, claimed)):
        print(line)
    print(f"pass: {time.monotonic() - began:.6f} s; {len(processes)} processes")


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("run", "scan"))
    _ = parser.add_argument("--proc-root", type=Path, default=build_hold.PROC_ROOT)
    options = parser.parse_args(arguments)
    proc_root = cast(Path, options.proc_root)
    try:
        if cast(str, options.command) == "scan":
            scan(proc_root, build_hold.holder_directory(), os.getuid(), os.getpid())
            return 0
        return run(proc_root, build_hold.holder_directory(), state_directory(),
                   uid=os.getuid(), enforcer_pid=os.getpid(), stopper=StopBuildsAndTests(proc_root))
    except (OSError, ValueError) as error:
        print(f"hold enforcer: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
