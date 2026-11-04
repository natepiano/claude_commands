#!/usr/bin/env python3
"""Share the location and state of one live verify call."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from typing import cast


@dataclass(frozen=True)
class MacAllowed:
    pass


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


@dataclass(frozen=True)
class SshAllowed:
    targets: tuple[str, ...]
    mac_targets: tuple[str, ...] = ()


RunsOn = MacAllowed | NatedevOnly | SshAllowed


@dataclass(frozen=True)
class Routed:
    pass


@dataclass(frozen=True)
class Pinned:
    target: str


TargetSelection = Routed | Pinned
ROUTED = Routed()


@dataclass(frozen=True)
class WorkspacePackages:
    """The command selects the workspace without a named package."""


@dataclass(frozen=True)
class NamedPackage:
    name: str


PackageSelection = WorkspacePackages | NamedPackage


def package_selection(value: PackageSelection | str | None) -> PackageSelection:
    return value if isinstance(value, (WorkspacePackages, NamedPackage)) else NamedPackage(value) if value else WorkspacePackages()


def package_name(value: PackageSelection) -> str:
    return value.name if isinstance(value, NamedPackage) else ""


def mac_eligible(record: StepRecord) -> bool:
    return isinstance(record.runs_on, MacAllowed) or (
        isinstance(record.runs_on, SshAllowed) and bool(record.runs_on.mac_targets))


class StepState(str, Enum):
    WAITING_HOLD = "waiting_hold"
    WAITING_MAC = "waiting_mac"
    WAITING_SSH = "waiting_ssh"
    ON_SSH = "on_ssh"
    ON_MAC = "on_mac"
    ON_NATEDEV = "on_natedev"
    LOCAL_NONCOMPILING = "local_noncompiling"


@dataclass(frozen=True, init=False)
class StepRecord:
    call_id: str
    pid: int
    proc_start: str
    repo: Path
    kind: str
    package: PackageSelection
    runs_on: RunsOn
    state: StepState
    updated: str
    v: int = 1
    selected_target: str = ""
    selection: TargetSelection = ROUTED

    def __init__(self, call_id: str, pid: int, proc_start: str, repo: Path, kind: str,
                 package: PackageSelection | str | None, runs_on: RunsOn, state: StepState,
                 updated: str, v: int = 1, selected_target: str = "",
                 selection: TargetSelection = ROUTED) -> None:
        for name, value in (("call_id", call_id), ("pid", pid), ("proc_start", proc_start),
                            ("repo", repo), ("kind", kind), ("package", package_selection(package)),
                            ("runs_on", runs_on), ("state", state), ("updated", updated),
                            ("v", v), ("selected_target", selected_target), ("selection", selection)):
            object.__setattr__(self, name, value)

    @property
    def eligible_targets(self) -> tuple[str, ...]:
        if isinstance(self.runs_on, SshAllowed):
            return self.runs_on.targets
        return ("mac", "natedev") if isinstance(self.runs_on, MacAllowed) else ("natedev",)


@dataclass(frozen=True)
class MissingRecord:
    call_id: str


@dataclass(frozen=True)
class StaleRecord:
    call_id: str


@dataclass(frozen=True)
class UnreadableRecord:
    call_id: str
    reason: str


ReadResult = StepRecord | MissingRecord | StaleRecord | UnreadableRecord
KINDS = ("check", "lint", "doc", "test", "build", "example", "final")


def record_directory() -> Path:
    return Path(os.environ.get("VERIFY_STEPS_DIR", str(Path.home() / ".local/state/verify-steps")))


def record_path(call_id: str) -> Path:
    if not call_id or Path(call_id).name != call_id or call_id in (".", ".."):
        raise ValueError("invalid call id")
    return record_directory() / f"{call_id}.json"


def process_start(pid: int) -> str:
    root = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
    fields = (root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] == "Z":
        raise ProcessLookupError("process has ended")
    return fields[19]


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("record needs nonempty text")
    return value


def _decode(value: object, call_id: str) -> StepRecord:
    if not isinstance(value, dict):
        raise ValueError("record must be an object")
    row = cast(dict[str, object], value)
    if row.get("v") != 1 or row.get("call_id") != call_id:
        raise ValueError("record version or call id differs")
    pid = row.get("pid")
    if type(pid) is not int or pid <= 0:
        raise ValueError("invalid process id")
    kind = _text(row.get("kind"))
    if kind not in KINDS:
        raise ValueError("invalid step kind")
    package = row.get("package")
    if package is not None and not isinstance(package, str):
        raise ValueError("invalid package")
    target = row.get("runs_on")
    if not isinstance(target, dict):
        raise ValueError("invalid run location")
    location = cast(dict[str, object], target)
    if location.get("kind") == "mac_allowed":
        runs_on: RunsOn = MacAllowed()
    elif location.get("kind") == "ssh_allowed":
        targets = row.get("eligible_targets")
        macs = location.get("mac_targets", [])
        if (not isinstance(targets, list) or not isinstance(macs, list)
                or not all(isinstance(item, str) and item for item in cast(list[object], targets))
                or not all(isinstance(item, str) and item for item in cast(list[object], macs))):
            raise ValueError("invalid eligible targets")
        runs_on = SshAllowed(tuple(cast(list[str], targets)), tuple(cast(list[str], macs)))
    elif location.get("kind") == "natedev_only":
        runs_on = NatedevOnly(_text(location.get("reason")))
    else:
        raise ValueError("invalid run location")
    selected = row.get("selected_target", "")
    if not isinstance(selected, str):
        raise ValueError("invalid selected target")
    pinned = row.get("pinned_target", "")
    if not isinstance(pinned, str):
        raise ValueError("invalid pinned target")
    if pinned and pinned not in (runs_on.targets if isinstance(runs_on, SshAllowed) else ("mac",)):
        raise ValueError("pinned target is not eligible")
    return StepRecord(call_id, pid, _text(row.get("proc_start")), Path(_text(row.get("repo"))),
                      kind, package, runs_on, StepState(_text(row.get("state"))), _text(row.get("updated")),
                      selected_target=selected, selection=Pinned(pinned) if pinned else Routed())


def read(call_id: str) -> ReadResult:
    try:
        path = record_path(call_id)
        value = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return MissingRecord(call_id)
    except (OSError, ValueError) as error:
        return UnreadableRecord(call_id, str(error))
    try:
        record = _decode(value, call_id)
    except (ValueError, TypeError) as error:
        return UnreadableRecord(call_id, str(error))
    try:
        start = process_start(record.pid)
    except (FileNotFoundError, ProcessLookupError):
        return StaleRecord(call_id)
    except (OSError, IndexError) as error:
        return UnreadableRecord(call_id, str(error))
    return record if start == record.proc_start else StaleRecord(call_id)


def live_records() -> list[StepRecord]:
    return [result for path in sorted(record_directory().glob("*.json"))
            if isinstance(result := read(path.stem), StepRecord)]


def write(record: StepRecord) -> None:
    path = record_path(record.call_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    location = ({"kind": "mac_allowed"} if isinstance(record.runs_on, MacAllowed)
                else {"kind": "ssh_allowed", "mac_targets": list(record.runs_on.mac_targets)}
                if isinstance(record.runs_on, SshAllowed)
                else {"kind": "natedev_only", "reason": record.runs_on.reason})
    row = dict(v=record.v, call_id=record.call_id, pid=record.pid, proc_start=record.proc_start,
               repo=str(record.repo), kind=record.kind, package=package_name(record.package) or None,
               eligible_targets=list(record.eligible_targets), selected_target=record.selected_target,
               runs_on=location, state=record.state.value, updated=record.updated)
    if isinstance(record.selection, Pinned):
        row["pinned_target"] = record.selection.target
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".step-", delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(row, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        _ = temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def state(call_id: str, next_state: StepState, target: str = "") -> None:
    record = read(call_id)
    if not isinstance(record, StepRecord):
        raise ValueError(f"cannot update step record: {record}")
    selected = target or ("mac" if next_state == StepState.ON_MAC else "natedev" if next_state == StepState.ON_NATEDEV else "")
    write(replace(record, state=next_state, selected_target=selected,
                  updated=datetime.now(timezone.utc).isoformat()))


def remove(call_id: str) -> None:
    record_path(call_id).unlink(missing_ok=True)


def consume_stopped_build(call_id: str, consumed: Path) -> bool:
    """Read this call's new enforcer stops, including stops before a release."""
    root = Path(os.environ.get("BUILDLOG_DIR", str(Path.home() / ".local/state/buildlog")))
    host = socket.gethostname().split(".")[0]
    proc = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
    boot_id = (proc / "sys/kernel/random/boot_id").read_text().strip()
    seen: set[str]
    try:
        seen = set(consumed.read_text().splitlines())
    except FileNotFoundError:
        seen = set()
    found: set[str] = set()
    for path in sorted((root / host).glob("hold-builds-*.jsonl")):
        for line in path.read_text().splitlines():
            value = cast(object, json.loads(line))
            if not isinstance(value, dict):
                continue
            row = cast(dict[str, object], value)
            if (row.get("kind") != "hold_build" or row.get("action") != "stopped"
                    or row.get("call_id") != call_id or row.get("boot_id") != boot_id):
                continue
            pid, start = row.get("pid"), row.get("proc_start")
            if type(pid) is not int or pid <= 0 or not isinstance(start, str) or not start:
                raise ValueError("stopped build has no process identity")
            identity = _text(row.get("id"))
            if identity not in seen:
                found.add(identity)
    if found:
        _ = consumed.write_text("\n".join(sorted(seen | found)) + "\n")
    return bool(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    _ = start.add_argument("pid", type=int)
    create = commands.add_parser("write")
    for flag in ("call-id", "pid", "kind", "runs-on", "state"):
        _ = create.add_argument("--" + flag, required=True)
    _ = create.add_argument("--repo", default=os.getcwd())
    _ = create.add_argument("--package")
    _ = create.add_argument("--reason", default="")
    _ = create.add_argument("--eligible-targets", default="")
    _ = create.add_argument("--mac-targets", default="")
    _ = create.add_argument("--pinned-target", default="")
    _ = create.add_argument("--clock-offset", action="store_true", help="print wall minus monotonic time in microseconds")
    update = commands.add_parser("state")
    _ = update.add_argument("--call-id", required=True)
    _ = update.add_argument("--target", default="")
    _ = update.add_argument("state", choices=[item.value for item in StepState])
    delete = commands.add_parser("remove")
    _ = delete.add_argument("--call-id", required=True)
    stopped = commands.add_parser("stopped")
    _ = stopped.add_argument("--call-id", required=True)
    _ = stopped.add_argument("--consumed", required=True)
    args = parser.parse_args()
    try:
        command = cast(str, args.command)
        if command == "start":
            print(process_start(cast(int, args.pid)))
            return 0
        call_id = cast(str, args.call_id)
        if command == "remove":
            remove(call_id)
        elif command == "stopped":
            return 0 if consume_stopped_build(call_id, Path(cast(str, args.consumed))) else 1
        elif command == "state":
            state(call_id, StepState(cast(str, args.state)), cast(str, args.target))
        else:
            pid = int(cast(str, args.pid))
            kind = cast(str, args.kind)
            location = cast(str, args.runs_on)
            reason = cast(str, args.reason)
            pinned_target = cast(str, args.pinned_target)
            if kind not in KINDS or location not in ("mac_allowed", "natedev_only", "ssh_allowed"):
                raise ValueError("invalid step kind or run location")
            if location == "natedev_only" and not reason:
                raise ValueError("natedev-only step needs a reason")
            write(StepRecord(call_id, pid, process_start(pid), Path(cast(str, args.repo)).resolve(), kind,
                             cast(str | None, args.package), MacAllowed() if location == "mac_allowed" else
                             SshAllowed(tuple(filter(None, cast(str, args.eligible_targets).split(","))),
                                        tuple(filter(None, cast(str, args.mac_targets).split(","))))
                             if location == "ssh_allowed" else NatedevOnly(reason),
                             StepState(cast(str, args.state)), datetime.now(timezone.utc).isoformat(),
                             selection=Pinned(pinned_target) if pinned_target else ROUTED))
            if cast(bool, args.clock_offset):
                print((time.time_ns() - time.monotonic_ns()) // 1000)
    except (OSError, ValueError, IndexError) as error:
        parser.exit(2, f"step record: {error}; step refused\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
