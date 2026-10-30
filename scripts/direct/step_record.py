#!/usr/bin/env python3
"""Share the location and state of one live verify call."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Literal, cast

KINDS = ("check", "lint", "doc", "test", "build", "example", "final")


class StepState(str, Enum):
    WAITING_HOLD = "waiting_hold"
    WAITING_MAC = "waiting_mac"
    ON_MAC = "on_mac"
    ON_NATEDEV = "on_natedev"
    LOCAL_NONCOMPILING = "local_noncompiling"


STATE_VALUES = tuple(item.value for item in StepState)


class _RecordUnavailable(Exception):
    def __init__(self, call_id: str, condition: Literal["missing", "stale", "unreadable"],
                 reason: str = "") -> None:
        super().__init__(reason)
        self.call_id: str = call_id
        self.condition: Literal["missing", "stale", "unreadable"] = condition
        self.reason: str = reason


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


def _validate_fields(value: object, call_id: str) -> dict[str, object]:
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
    if location.get("kind") == "natedev_only":
        _ = _text(location.get("reason"))
    elif location.get("kind") != "mac_allowed":
        raise ValueError("invalid run location")
    for field in ("proc_start", "repo", "updated"):
        _ = _text(row.get(field))
    state_value = _text(row.get("state"))
    if state_value not in STATE_VALUES:
        raise ValueError(f"{state_value!r} is not a valid StepState")
    return dict(v=1, call_id=call_id, pid=pid, proc_start=row["proc_start"],
                repo=str(Path(cast(str, row["repo"]))), kind=row["kind"], package=package,
                runs_on={"kind": location["kind"],
                         **({"reason": location["reason"]} if location["kind"] == "natedev_only" else {})},
                state=state_value, updated=row["updated"])


def _write_fields(call_id: str, row: dict[str, object]) -> None:
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


def _read_fields(call_id: str) -> dict[str, object]:
    try:
        value = cast(object, json.loads(record_path(call_id).read_text()))
    except FileNotFoundError as error:
        raise _RecordUnavailable(call_id, "missing") from error
    except (OSError, ValueError) as error:
        raise _RecordUnavailable(call_id, "unreadable", str(error)) from error
    try:
        row = _validate_fields(value, call_id)
    except (ValueError, TypeError) as error:
        raise _RecordUnavailable(call_id, "unreadable", str(error)) from error
    try:
        start = process_start(cast(int, row["pid"]))
    except (FileNotFoundError, ProcessLookupError) as error:
        raise _RecordUnavailable(call_id, "stale") from error
    except (OSError, IndexError) as error:
        raise _RecordUnavailable(call_id, "unreadable", str(error)) from error
    if start != row["proc_start"]:
        raise _RecordUnavailable(call_id, "stale")
    return row


def _update_state(call_id: str, next_state: StepState) -> None:
    row = _read_fields(call_id)
    row["state"] = next_state.value
    row["updated"] = datetime.now(timezone.utc).isoformat()
    _write_fields(call_id, row)


def remove(call_id: str) -> None:
    record_path(call_id).unlink(missing_ok=True)


def consume_stopped_build(call_id: str, consumed: Path) -> bool:
    """Read this call's new enforcer stops, including stops before a release."""
    import socket

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


class _CommandArgument:
    def __init__(self, name: str, *, required: bool = False, choices: tuple[str, ...] = (),
                 switch: bool = False, help_text: str = "") -> None:
        self.name: str = name
        self.required: bool = required
        self.choices: tuple[str, ...] = choices
        self.switch: bool = switch
        self.help_text: str = help_text


_CALL_ID = _CommandArgument("--call-id", required=True)
_COMMAND_ARGUMENTS = {
    "write": (_CALL_ID, *(_CommandArgument("--" + name, required=True)
                         for name in ("pid", "kind", "runs-on", "state")),
              *(_CommandArgument("--" + name) for name in ("repo", "package", "reason")),
              _CommandArgument("--clock-offset", switch=True,
                               help_text="print wall minus monotonic microseconds after writing")),
    "state": (_CALL_ID, _CommandArgument("state", required=True, choices=STATE_VALUES)),
    "remove": (_CALL_ID,),
    "stopped": (_CALL_ID, _CommandArgument("--consumed", required=True)),
}


class _NeedsArgumentParser(Exception):
    """Help or syntax outside the ordinary flag/value form needs argparse."""


def _parse_record_arguments(arguments: list[str]) -> tuple[str, dict[str, str]]:
    if not arguments or arguments[0] not in _COMMAND_ARGUMENTS:
        raise _NeedsArgumentParser
    command, *arguments = arguments
    definitions = {argument.name: argument for argument in _COMMAND_ARGUMENTS[command]}
    values: dict[str, str] = {}
    while arguments:
        argument, *arguments = arguments
        definition = definitions.get(argument)
        if definition is not None and definition.name.startswith("--"):
            if definition.switch:
                values[argument[2:]] = "true"
            elif arguments and not arguments[0].startswith("-"):
                values[argument[2:]], *arguments = arguments
            else:
                raise _NeedsArgumentParser
        else:
            positional = next((item for item in definitions.values()
                               if not item.name.startswith("--") and item.name not in values), None)
            if positional is None or argument not in positional.choices:
                raise _NeedsArgumentParser
            values[positional.name] = argument
    if any(item.required and item.name.removeprefix("--") not in values
           for item in definitions.values()):
        raise _NeedsArgumentParser
    return command, values


def _execute_record_command(command: str, values: dict[str, str]) -> int:
    call_id = values["call-id"]
    if command == "write":
        pid = int(values["pid"])
        kind, location = values["kind"], values["runs-on"]
        reason = values.get("reason", "")
        if kind not in KINDS or location not in ("mac_allowed", "natedev_only"):
            raise ValueError("invalid step kind or run location")
        if location == "natedev_only" and not reason:
            raise ValueError("natedev-only step needs a reason")
        start = process_start(pid)
        repo = Path(values.get("repo", os.getcwd())).resolve()
        row: dict[str, object] = dict(
            v=1, call_id=call_id, pid=pid, proc_start=start, repo=str(repo), kind=kind,
            package=values.get("package"),
            runs_on={"kind": location, **({"reason": reason} if location == "natedev_only" else {})},
            state=StepState(values["state"]).value, updated=datetime.now(timezone.utc).isoformat())
        _write_fields(call_id, row)
        if "clock-offset" in values:
            import time

            print((time.time_ns() - time.monotonic_ns()) // 1000)
    elif command == "state":
        _update_state(call_id, StepState(values["state"]))
    elif command == "remove":
        remove(call_id)
    else:
        return 0 if consume_stopped_build(call_id, Path(values["consumed"])) else 1
    return 0


def _command_status(command: str, values: dict[str, str]) -> int:
    try:
        return _execute_record_command(command, values)
    except (OSError, ValueError, IndexError) as error:
        print(f"step record: {error}; step refused", file=sys.stderr)
        return 2


def _run_record_command() -> None:
    try:
        command, values = _parse_record_arguments(sys.argv[1:])
        status = _command_status(command, values)
    except (_NeedsArgumentParser, _RecordUnavailable):
        # Help, unusual syntax and unavailable-record diagnostics load the public API.
        return
    raise SystemExit(status)


# CLI mutations need neither the public dataclasses nor argparse's help machinery.
if __name__ == "__main__":
    _run_record_command()


from dataclasses import dataclass


@dataclass(frozen=True)
class MacAllowed:
    pass


@dataclass(frozen=True)
class NatedevOnly:
    reason: str


RunsOn = MacAllowed | NatedevOnly


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


def _decode(row: dict[str, object], call_id: str) -> StepRecord:
    location = cast(dict[str, object], row["runs_on"])
    runs_on: RunsOn = (MacAllowed() if location["kind"] == "mac_allowed"
                      else NatedevOnly(cast(str, location["reason"])))
    return StepRecord(call_id, cast(int, row["pid"]), cast(str, row["proc_start"]),
                      Path(cast(str, row["repo"])), cast(str, row["kind"]),
                      cast(str | None, row["package"]), runs_on, StepState(cast(str, row["state"])),
                      cast(str, row["updated"]))


def _unavailable_result(error: _RecordUnavailable) -> MissingRecord | StaleRecord | UnreadableRecord:
    if error.condition == "missing":
        return MissingRecord(error.call_id)
    if error.condition == "stale":
        return StaleRecord(error.call_id)
    return UnreadableRecord(error.call_id, error.reason)


def read(call_id: str) -> ReadResult:
    try:
        return _decode(_read_fields(call_id), call_id)
    except _RecordUnavailable as error:
        return _unavailable_result(error)


def live_records() -> list[StepRecord]:
    return [result for path in sorted(record_directory().glob("*.json"))
            if isinstance(result := read(path.stem), StepRecord)]


def write(record: StepRecord) -> None:
    location = ({"kind": "mac_allowed"} if isinstance(record.runs_on, MacAllowed)
                else {"kind": "natedev_only", "reason": record.runs_on.reason})
    row: dict[str, object] = dict(v=record.v, call_id=record.call_id, pid=record.pid,
               proc_start=record.proc_start, repo=str(record.repo), kind=record.kind,
               package=record.package, runs_on=location, state=record.state.value, updated=record.updated)
    _write_fields(record.call_id, row)


def state(call_id: str, next_state: StepState) -> None:
    try:
        _update_state(call_id, next_state)
    except _RecordUnavailable as error:
        raise ValueError(f"cannot update step record: {_unavailable_result(error)}") from error


def _parse_with_argparse(arguments: list[str]) -> tuple[str, dict[str, str]]:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, definitions in _COMMAND_ARGUMENTS.items():
        subparser = commands.add_parser(command)
        for argument in definitions:
            if argument.switch:
                _ = subparser.add_argument(argument.name, action="store_true", help=argument.help_text)
            elif argument.name.startswith("--"):
                _ = subparser.add_argument(argument.name, required=argument.required)
            else:
                _ = subparser.add_argument(argument.name, choices=argument.choices)
    args = cast(dict[str, object], vars(parser.parse_args(arguments)))
    command = cast(str, args.pop("command"))
    values = {name.replace("_", "-"): "true" if value is True else cast(str, value)
              for name, value in args.items() if value is not None and value is not False}
    return command, values


def main() -> int:
    try:
        command, values = _parse_record_arguments(sys.argv[1:])
    except _NeedsArgumentParser:
        command, values = _parse_with_argparse(sys.argv[1:])
    try:
        return _command_status(command, values)
    except _RecordUnavailable as error:
        print(f"step record: cannot update step record: {_unavailable_result(error)}; step refused",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
