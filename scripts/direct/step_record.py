#!/usr/bin/env python3
"""Share the location and state of one live verify call."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from typing import cast

from scripts.lib import process_start as process_identity

from scripts.lib.boot_identity import boot_identity


KINDS = ("check", "lint", "doc", "test", "build", "example", "final")
STATES = ("waiting_hold", "waiting_mac", "waiting_ssh", "on_ssh", "on_mac", "on_natedev", "local_noncompiling")
# Each rule holds the argument name, requirement, default and choices.
COMMAND_ARGUMENTS: dict[str, tuple[tuple[str, bool, str, tuple[str, ...]], ...]] = {
    "start": (("pid", True, "", ()),),
    "write": tuple(("--" + name, True, "", ()) for name in ("call-id", "pid", "kind", "runs-on", "state"))
        + (("--repo", False, os.getcwd(), ()),)
        + tuple(("--" + name, False, "", ()) for name in
                ("package", "reason", "eligible-targets", "mac-targets", "mac-disk-exclusions", "pinned-target")),
    "stopped": (("--call-id", True, "", ()), ("--consumed", True, "", ())),
}


class UseArgumentParser:
    """Help or unsupported syntax requires the full argument parser."""


def parse_light_cli(arguments: list[str]) -> dict[str, str] | UseArgumentParser:
    if not arguments or arguments[0] not in ("write", "stopped"):
        return UseArgumentParser()
    verb = arguments[0]
    rules = COMMAND_ARGUMENTS[verb]
    flags = {name: (required, default, choices) for name, required, default, choices in rules if name.startswith("--")}
    positional = [name for name, _, _, _ in rules if not name.startswith("--")]
    values = {name.removeprefix("--").replace("-", "_"): default
              for name, required, default, _ in rules if not required}
    values["command"] = verb
    index = 1
    while index < len(arguments):
        token = arguments[index]
        if verb == "write" and token == "--report":
            values["report"] = "1"
            index += 1
            continue
        if token.startswith("--"):
            name, separator, value = token.partition("=")
            if name not in flags:
                return UseArgumentParser()
            if not separator:
                index += 1
                if index == len(arguments) or arguments[index].startswith("-"):
                    return UseArgumentParser()
                value = arguments[index]
            values[name[2:].replace("-", "_")] = value
        elif token.startswith("-") or not positional:
            return UseArgumentParser()
        else:
            values[positional.pop(0)] = token
        index += 1
    for name, required, _, choices in rules:
        key = name.removeprefix("--").replace("-", "_")
        if (required and key not in values) or (choices and values.get(key) not in choices):
            return UseArgumentParser()
    return values


def execute_cli(args: dict[str, str]) -> int:
    try:
        command = args["command"]
        if command == "start":
            print(process_start(int(args["pid"])))
            return 0
        call_id = args["call_id"]
        if command == "stopped":
            return 0 if consume_stopped_build(call_id, Path(args["consumed"])) else 1
        else:
            pid, kind, location = int(args["pid"]), args["kind"], args["runs_on"]
            if kind not in KINDS or location not in ("any_target", "natedev_only"):
                raise ValueError("invalid step kind or run location")
            if location == "natedev_only" and not args["reason"]:
                raise ValueError("natedev-only step needs a reason")
            if args["state"] not in STATES:
                raise ValueError("invalid step state")
            runs_on: dict[str, object] = {"kind": location}
            eligible = ["natedev"]
            if location == "any_target":
                eligible = list(filter(None, args["eligible_targets"].split(",")))
                runs_on["mac_targets"] = list(filter(None, args["mac_targets"].split(",")))
            elif location == "natedev_only":
                runs_on["reason"] = args["reason"]
            row: dict[str, object] = dict(v=1, call_id=call_id, pid=pid, proc_start=process_start(pid),
                repo=str(Path(args["repo"]).resolve()), kind=kind, package=args["package"] or None,
                eligible_targets=eligible, selected_target="", runs_on=runs_on, state=args["state"],
                mac_disk_exclusions=list(filter(None, args["mac_disk_exclusions"].split(","))),
                updated=datetime.now(timezone.utc).isoformat())
            if args["pinned_target"]:
                row["pinned_target"] = args["pinned_target"]
            write_fields(call_id, row)
            if args.get("report"):
                print((time.time_ns() - time.monotonic_ns()) // 1000)
                print(record_path(call_id))
                print(state_path(call_id))
    except (OSError, ValueError, IndexError) as error:
        print(f"step record: {error}; step refused", file=sys.stderr)
        return 2
    return 0


def record_directory() -> Path:
    return Path(os.environ.get("VERIFY_STEPS_DIR", str(Path.home() / ".local/state/verify-steps")))


def record_path(call_id: str) -> Path:
    if not call_id or Path(call_id).name != call_id or call_id in (".", ".."):
        raise ValueError("invalid call id")
    return record_directory() / f"{call_id}.json"


def state_path(call_id: str) -> Path:
    return record_path(call_id).with_suffix(".state")


def process_start(pid: int) -> str:
    result = process_identity.process_start(pid)
    if isinstance(result, process_identity.Started):
        return result.start
    if isinstance(result, (process_identity.Gone, process_identity.Zombie)):
        raise ProcessLookupError("process has ended")
    raise OSError(result.reason)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("record needs nonempty text")
    return value


def validated_fields(value: object, call_id: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("record must be an object")
    row = cast(dict[str, object], value)
    if row.get("v") != 1 or row.get("call_id") != call_id:
        raise ValueError("record version or call id differs")
    pid = row.get("pid")
    if type(pid) is not int or pid <= 0:
        raise ValueError("invalid process id")
    if _text(row.get("kind")) not in KINDS:
        raise ValueError("invalid step kind")
    package = row.get("package")
    if package is not None and not isinstance(package, str):
        raise ValueError("invalid package")
    target = row.get("runs_on")
    if not isinstance(target, dict):
        raise ValueError("invalid run location")
    location = cast(dict[str, object], target)
    if location.get("kind") == "any_target":
        targets, macs = row.get("eligible_targets"), location.get("mac_targets", [])
        if (not isinstance(targets, list) or not isinstance(macs, list)
                or not all(isinstance(item, str) and item for item in cast(list[object], targets))
                or not all(isinstance(item, str) and item for item in cast(list[object], macs))):
            raise ValueError("invalid eligible targets")
        eligible = cast(list[str], targets)
    elif location.get("kind") == "natedev_only":
        _ = _text(location.get("reason"))
        eligible = ["natedev"]
    else:
        raise ValueError("invalid run location")
    selected, pinned = row.get("selected_target", ""), row.get("pinned_target", "")
    if not isinstance(selected, str):
        raise ValueError("invalid selected target")
    if not isinstance(pinned, str):
        raise ValueError("invalid pinned target")
    if pinned and pinned not in eligible:
        raise ValueError("pinned target is not eligible")
    for field in ("proc_start", "repo", "updated"):
        _ = _text(row.get(field))
    if _text(row.get("state")) not in STATES:
        raise ValueError("invalid step state")
    return {**row, "eligible_targets": eligible, "selected_target": selected, "package": package or None}


def write_fields(call_id: str, row: Mapping[str, object]) -> None:
    path = record_path(call_id)
    path.parent.mkdir(parents=True, exist_ok=True)
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


def consume_stopped_build(call_id: str, consumed: Path) -> bool:
    """Read this call's new enforcer stops, including stops before a release."""
    root = Path(os.environ.get("BUILDLOG_DIR", str(Path.home() / ".local/state/buildlog")))
    host = socket.gethostname().split(".")[0]
    proc = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
    if sys.platform == "darwin":
        return False  # The Linux build enforcer does not run on Darwin.
    boot_id = boot_identity(proc)
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


if __name__ == "__main__":
    parsed = parse_light_cli(sys.argv[1:])
    if isinstance(parsed, dict):
        raise SystemExit(execute_cli(parsed))


from dataclasses import dataclass
from scripts.direct.step_selection import (
    Pinned as Pinned,
    ROUTED as ROUTED,
    Routed as Routed,
    TargetSelection as TargetSelection,
)


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


@dataclass(frozen=True)
class AnyTarget:
    targets: tuple[str, ...]
    mac_targets: tuple[str, ...] = ()


RunsOn = NatedevOnly | AnyTarget


@dataclass(frozen=True)
class WorkspacePackages:
    """The command selects the workspace without a named package."""


@dataclass(frozen=True)
class NamedPackage:
    name: str


PackageSelection = WorkspacePackages | NamedPackage


def package_selection(value: str | None) -> PackageSelection:
    return NamedPackage(value) if value else WorkspacePackages()


def package_name(value: PackageSelection) -> str:
    return value.name if isinstance(value, NamedPackage) else ""


def mac_eligible(record: StepRecord) -> bool:
    return isinstance(record.runs_on, AnyTarget) and bool(record.runs_on.mac_targets)


class StepState(str, Enum):
    WAITING_HOLD = "waiting_hold"
    WAITING_MAC = "waiting_mac"
    WAITING_SSH = "waiting_ssh"
    ON_SSH = "on_ssh"
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
    package: PackageSelection
    runs_on: RunsOn
    state: StepState
    updated: str
    v: int = 1
    selected_target: str = ""
    selection: TargetSelection = ROUTED
    mac_disk_exclusions: tuple[str, ...] = ()

    @property
    def eligible_targets(self) -> tuple[str, ...]:
        if isinstance(self.runs_on, AnyTarget):
            return self.runs_on.targets
        return ("natedev",)


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


def _decode(value: object, call_id: str) -> StepRecord:
    row = validated_fields(value, call_id)
    location = cast(dict[str, object], row["runs_on"])
    runs_on: RunsOn
    if location["kind"] == "any_target":
        runs_on = AnyTarget(tuple(cast(list[str], row["eligible_targets"])),
                            tuple(cast(list[str], location.get("mac_targets", []))))
    else:
        runs_on = NatedevOnly(_text(location["reason"]))
    pinned = cast(str, row.get("pinned_target", ""))
    return StepRecord(call_id, cast(int, row["pid"]), _text(row["proc_start"]), Path(_text(row["repo"])),
                      _text(row["kind"]), package_selection(cast(str | None, row["package"])), runs_on,
                      StepState(_text(row["state"])), _text(row["updated"]),
                      selected_target=cast(str, row["selected_target"]), selection=Pinned(pinned) if pinned else ROUTED,
                      mac_disk_exclusions=tuple(cast(list[str], row.get("mac_disk_exclusions", []))))


def read(call_id: str) -> ReadResult:
    try:
        state_file = state_path(call_id).open()
    except FileNotFoundError:
        state_file = None
    except (OSError, ValueError) as error:
        return UnreadableRecord(call_id, str(error))
    try:
        try:
            value = cast(object, json.loads(record_path(call_id).read_text()))
        except FileNotFoundError:
            return MissingRecord(call_id)
        except (OSError, ValueError) as error:
            return UnreadableRecord(call_id, str(error))
        try:
            if state_file is not None:
                words = state_file.read().split()
                updated = datetime.fromtimestamp(os.fstat(state_file.fileno()).st_mtime, timezone.utc).isoformat()
                if not isinstance(value, dict):
                    raise ValueError("record must be an object")
                row = cast(dict[str, object], value)
                value = row
                if words and words[0] == "writes_tree":
                    row["runs_on"] = {"kind": "natedev_only", "reason": "step writes the tree"}
                    if row.get("pinned_target"):
                        row["pinned_target"] = "natedev"
                    words = words[1:]
                word = words[0] if words else ""
                selected = words[1] if len(words) > 1 else "mac" if word == "on_mac" else "natedev" if word == "on_natedev" else ""
                row.update(state=word, selected_target=selected, updated=updated)
            record = _decode(value, call_id)
        except (OSError, ValueError, TypeError) as error:
            return UnreadableRecord(call_id, str(error))
    finally:
        if state_file is not None:
            state_file.close()
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
    location = ({"kind": "any_target", "mac_targets": list(record.runs_on.mac_targets)}
                if isinstance(record.runs_on, AnyTarget)
                else {"kind": "natedev_only", "reason": record.runs_on.reason})
    row = dict(v=record.v, call_id=record.call_id, pid=record.pid, proc_start=record.proc_start,
               repo=str(record.repo), kind=record.kind, package=package_name(record.package) or None,
               eligible_targets=list(record.eligible_targets), selected_target=record.selected_target,
               mac_disk_exclusions=list(record.mac_disk_exclusions),
               runs_on=location, state=record.state.value, updated=record.updated)
    if isinstance(record.selection, Pinned):
        row["pinned_target"] = record.selection.target
    write_fields(record.call_id, row)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for verb, rules in COMMAND_ARGUMENTS.items():
        command = commands.add_parser(verb)
        for name, required, default, choices in rules:
            if name.startswith("--"):
                _ = command.add_argument(name, required=required, default=default, choices=choices or None)
            else:
                _ = command.add_argument(name, choices=choices or None)
        if verb == "write":
            _ = command.add_argument("--report", action="store_const", const="1", default="",
                                     help="print the clock offset, record path and state path")
    arguments = vars(parser.parse_args())
    return execute_cli(cast(dict[str, str], arguments))


if __name__ == "__main__":
    raise SystemExit(main())
