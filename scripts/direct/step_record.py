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
import sys
import tempfile
from typing import cast


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import process_start


@dataclass(frozen=True)
class MacAllowed:
    pass


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


RunsOn = MacAllowed | NatedevOnly


class StepState(str, Enum):
    WAITING_HOLD = "waiting_hold"
    WAITING_MAC = "waiting_mac"
    ON_MAC = "on_mac"
    ON_NATEDEV = "on_natedev"
    LOCAL_NONCOMPILING = "local_noncompiling"


@dataclass(frozen=True)
class StepRecord:
    call_id: str
    pid: int
    proc_start: str
    repo: Path
    kind: str
    package: str | None
    runs_on: RunsOn
    state: StepState
    updated: str
    v: int = 1


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
    elif location.get("kind") == "natedev_only":
        runs_on = NatedevOnly(_text(location.get("reason")))
    else:
        raise ValueError("invalid run location")
    return StepRecord(call_id, pid, _text(row.get("proc_start")), Path(_text(row.get("repo"))),
                      kind, package, runs_on, StepState(_text(row.get("state"))), _text(row.get("updated")))


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
        recorded_start = process_start.parse_start(record.proc_start)
    except (ValueError, TypeError) as error:
        return UnreadableRecord(call_id, str(error))
    owner = process_start.check_owner(record.pid, recorded_start,
                                      Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")))
    if owner in (process_start.OwnerCheck.GONE, process_start.OwnerCheck.REPLACED):
        return StaleRecord(call_id)
    if owner is process_start.OwnerCheck.UNKNOWN:
        return UnreadableRecord(call_id, "process owner cannot be confirmed")
    return record


def live_records() -> list[StepRecord]:
    return [result for path in sorted(record_directory().glob("*.json"))
            if isinstance(result := read(path.stem), StepRecord)]


def write(record: StepRecord) -> None:
    path = record_path(record.call_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    location = ({"kind": "mac_allowed"} if isinstance(record.runs_on, MacAllowed)
                else {"kind": "natedev_only", "reason": record.runs_on.reason})
    row = dict(v=record.v, call_id=record.call_id, pid=record.pid, proc_start=record.proc_start,
               repo=str(record.repo), kind=record.kind, package=record.package,
               runs_on=location, state=record.state.value, updated=record.updated)
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


def state(call_id: str, next_state: StepState) -> None:
    record = read(call_id)
    if not isinstance(record, StepRecord):
        raise ValueError(f"cannot update step record: {record}")
    write(replace(record, state=next_state, updated=datetime.now(timezone.utc).isoformat()))


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
    create = commands.add_parser("write")
    for flag in ("call-id", "pid", "kind", "runs-on", "state"):
        _ = create.add_argument("--" + flag, required=True)
    _ = create.add_argument("--repo", default=os.getcwd())
    _ = create.add_argument("--package")
    _ = create.add_argument("--reason", default="")
    update = commands.add_parser("state")
    _ = update.add_argument("--call-id", required=True)
    _ = update.add_argument("state", choices=[item.value for item in StepState])
    delete = commands.add_parser("remove")
    _ = delete.add_argument("--call-id", required=True)
    stopped = commands.add_parser("stopped")
    _ = stopped.add_argument("--call-id", required=True)
    _ = stopped.add_argument("--consumed", required=True)
    args = parser.parse_args()
    try:
        call_id = cast(str, args.call_id)
        command = cast(str, args.command)
        if command == "remove":
            remove(call_id)
        elif command == "stopped":
            return 0 if consume_stopped_build(call_id, Path(cast(str, args.consumed))) else 1
        elif command == "state":
            state(call_id, StepState(cast(str, args.state)))
        else:
            pid = int(cast(str, args.pid))
            kind = cast(str, args.kind)
            location = cast(str, args.runs_on)
            reason = cast(str, args.reason)
            if kind not in KINDS or location not in ("mac_allowed", "natedev_only"):
                raise ValueError("invalid step kind or run location")
            if location == "natedev_only" and not reason:
                raise ValueError("natedev-only step needs a reason")
            started = process_start.process_start(pid, Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")))
            if isinstance(started, process_start.ProcessGone):
                raise ValueError("process has ended")
            write(StepRecord(call_id, pid, started.text, Path(cast(str, args.repo)).resolve(), kind,
                             cast(str | None, args.package), MacAllowed() if location == "mac_allowed" else NatedevOnly(reason),
                             StepState(cast(str, args.state)), datetime.now(timezone.utc).isoformat()))
    except (OSError, ValueError, IndexError) as error:
        parser.exit(2, f"step record: {error}; step refused\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
