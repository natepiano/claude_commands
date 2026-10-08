#!/usr/bin/env python3
"""Maintain the machine-local showrunner registry."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

CONFIG = Path(os.environ.get("SHOWRUNNERS_CONFIG") or Path(__file__).resolve().parents[2] / "config/showrunners.json")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
SESSIONS = Path(os.environ.get("SHOWRUNNERS_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")


class RunningUnitDirector(NamedTuple):
    session: str


class RunFinishedUnitDirector(NamedTuple):
    session: str


class StandingByUnitDirector(NamedTuple):
    session: str


RegisteredUnitDirector = RunningUnitDirector | RunFinishedUnitDirector | StandingByUnitDirector


class Showrunner(TypedDict):
    session: str
    zone: str
    units: list[RegisteredUnitDirector]


class ShowrunnerSettings(TypedDict):
    threshold_percent: float
    repeat_minutes: float
    stall_minutes: float
    faults_to: str
    always: list[str]
    showrunners: list[Showrunner]


class RunningShowrunner(NamedTuple):
    slug: str
    session: str
    socket: str
    prompt: PromptUnits | UnreadablePrompt


class PromptUnits(NamedTuple):
    zone: str
    unit_sessions: tuple[str, ...]


class UnreadablePrompt(NamedTuple):
    reason: str


class CheckedDoc(NamedTuple):
    path: Path


class NoCheckedDoc(NamedTuple):
    reason: str


def defaults() -> ShowrunnerSettings:
    return ShowrunnerSettings(threshold_percent=2, repeat_minutes=30, stall_minutes=5,
                              faults_to="natedev", always=["natedev"], showrunners=[])


def _unit_director(session: str, status: str) -> RegisteredUnitDirector:
    if not session:
        raise ValueError("invalid unit session")
    if status == "running":
        return RunningUnitDirector(session)
    if status == "run-finished":
        return RunFinishedUnitDirector(session)
    if status == "standing-by":
        return StandingByUnitDirector(session)
    raise ValueError(f"invalid unit status: {status}")


def _unit_status(unit: RegisteredUnitDirector) -> str:
    if isinstance(unit, RunningUnitDirector):
        return "running"
    if isinstance(unit, RunFinishedUnitDirector):
        return "run-finished"
    return "standing-by"


def load_settings() -> ShowrunnerSettings:
    return load_settings_from(CONFIG)


def load_settings_from(path: Path) -> ShowrunnerSettings:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            raise ValueError("expected an object")
        data = cast(dict[str, object], raw)
        for key in ("threshold_percent", "repeat_minutes", "stall_minutes"):
            if not isinstance(data.get(key), (int, float)) or isinstance(data[key], bool):
                raise ValueError(f"invalid {key}")
        if not isinstance(data.get("faults_to"), str) or not data["faults_to"]:
            raise ValueError("invalid faults_to")
        always, runners = data.get("always"), data.get("showrunners")
        if not isinstance(always, list) or not all(isinstance(item, str) for item in cast(list[object], always)):
            raise ValueError("invalid always")
        if not isinstance(runners, list):
            raise ValueError("invalid showrunners")
        checked: list[Showrunner] = []
        for item in cast(list[object], runners):
            if not isinstance(item, dict):
                raise ValueError("invalid showrunner")
            entry = cast(dict[str, object], item)
            session, zone, units = entry.get("session"), entry.get("zone"), entry.get("units")
            standby = entry.get("standby", [])
            if (not isinstance(session, str) or not session or not isinstance(zone, str)
                    or not isinstance(units, list)
                    or not isinstance(standby, list)
                    or not all(isinstance(unit, str) for unit in cast(list[object], standby))):
                raise ValueError("invalid showrunner fields")
            waiting = cast(list[str], standby)
            if len(set(waiting)) != len(waiting):
                raise ValueError("invalid standby units")
            checked_units: list[RegisteredUnitDirector] = []
            legacy_sessions: set[str] = set()
            for raw_unit in cast(list[object], units):
                if isinstance(raw_unit, str):
                    legacy_sessions.add(raw_unit)
                    checked_units.append(StandingByUnitDirector(raw_unit)
                                         if raw_unit in waiting else RunningUnitDirector(raw_unit))
                    continue
                if not isinstance(raw_unit, dict):
                    raise ValueError("invalid unit")
                stored_unit = cast(dict[str, object], raw_unit)
                unit_session, status = stored_unit.get("session"), stored_unit.get("status")
                if not isinstance(unit_session, str) or not isinstance(status, str):
                    raise ValueError("invalid unit")
                checked_units.append(_unit_director(unit_session, status))
            unit_sessions = [unit.session for unit in checked_units]
            if (len(set(unit_sessions)) != len(unit_sessions)
                    or not set(waiting) <= legacy_sessions):
                raise ValueError("invalid unit sessions")
            _ = ZoneInfo(zone)
            checked.append(Showrunner(session=session, zone=zone, units=checked_units))
        return ShowrunnerSettings(threshold_percent=cast(float, data["threshold_percent"]),
                                  repeat_minutes=cast(float, data["repeat_minutes"]),
                                  stall_minutes=cast(float, data["stall_minutes"]),
                                  faults_to=cast(str, data["faults_to"]), always=cast(list[str], always),
                                  showrunners=checked)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(f"{path}: {error}") from error


def socket_for(target: str) -> str | None:
    """The session's socket, or None when it is not running. Raises OSError when the session
    records cannot be read, which says nothing about whether it runs."""
    result = subprocess.run([sys.executable, str(SESSIONS), "socket", target], capture_output=True,
                            text=True, check=False)
    # Exit 1 is no such live session. Any other failure is the lookup itself failing.
    if result.returncode not in (0, 1):
        reason = result.stderr.strip() or f"sessions.py exited {result.returncode}"
        raise OSError(f"cannot tell whether {target} is running: {reason}")
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _fields(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def checked_doc(instance: Path) -> CheckedDoc | NoCheckedDoc:
    """Read the production doc passed to an update instance's production check."""
    try:
        check = _fields(instance / "conf").get("CHECK")
    except (OSError, UnicodeError) as error:
        return NoCheckedDoc(f"conf could not be read: {error}")
    if check is None:
        return NoCheckedDoc("CHECK is missing")
    try:
        command = shlex.split(check)
    except ValueError as error:
        return NoCheckedDoc(f"CHECK could not be parsed: {error}")
    try:
        script_index = next(index for index, token in enumerate(command)
                            if Path(token).name == "production_check.sh")
    except StopIteration:
        return NoCheckedDoc("CHECK has no production_check.sh token")
    if script_index + 1 >= len(command):
        return NoCheckedDoc("CHECK has no production doc token")
    path = Path(command[script_index + 1])
    if not path.is_absolute():
        return NoCheckedDoc("production doc path is relative")
    return CheckedDoc(path)


def _session_name(session_id: str) -> str:
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(path.stem)
            os.kill(pid, 0)
            data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            name = data.get("name")
            if data.get("sessionId") == session_id and isinstance(name, str) and name:
                return name
        except (OSError, ValueError, TypeError):
            continue
    raise ValueError(f"session {session_id} has no live named process")


def _prompt_units(fields: dict[str, str]) -> PromptUnits | UnreadablePrompt:
    value = fields.get("PROMPT_FILE")
    if not value:
        return UnreadablePrompt("PROMPT_FILE is missing")
    prompt_file = Path(value)
    if not prompt_file.is_absolute():
        return UnreadablePrompt("PROMPT_FILE is not absolute")
    try:
        prompt = prompt_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return UnreadablePrompt(f"prompt could not be read: {error}")
    match = re.search(r"unit_status\.sh\s+\S+\s+([^\s|`]+)\s+([^|`\n]+)", prompt)
    if match is None:
        return UnreadablePrompt("unit_status.sh command is missing its zone or units")
    zone = match.group(1)
    try:
        _ = ZoneInfo(zone)
        units = tuple(shlex.split(match.group(2).strip()))
    except (KeyError, ValueError) as error:
        return UnreadablePrompt(f"prompt has invalid zone or units: {error}")
    if units[:1] == ("--showrunner",):
        if len(units) != 2:
            return UnreadablePrompt("unit_status.sh --showrunner needs a session")
        return PromptUnits(zone, ())
    if not units:
        return UnreadablePrompt("unit_status.sh command has no units")
    return PromptUnits(zone, units)


def _running(instance: Path) -> RunningShowrunner | None:
    fields = _fields(instance / "conf")
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    socket = socket_for(target)
    if socket is None:
        return None
    return RunningShowrunner(instance.name.removeprefix("showrunner-"), _session_name(target[8:]),
                             socket, _prompt_units(fields))


def running_showrunners(state_dir: Path | None = None) -> list[RunningShowrunner]:
    found: list[RunningShowrunner] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        if not instance.is_dir():
            continue
        try:
            runner = _running(instance)
            if runner is not None:
                found.append(runner)
        except (OSError, KeyError, ValueError) as error:
            print(f"showrunners: skipping {instance.name}: {error}", file=sys.stderr)
    return found


def missing_showrunners(settings: ShowrunnerSettings) -> list[RunningShowrunner]:
    sockets = {socket for runner in settings["showrunners"]
               if (socket := socket_for(runner["session"])) is not None}
    return [runner for runner in running_showrunners() if runner.socket not in sockets]


def add(settings: ShowrunnerSettings, session: str, zone: str, units: list[str], standby: bool = False) -> None:
    _ = ZoneInfo(zone)
    requested: list[RegisteredUnitDirector] = [
        _unit_director(unit, "standing-by" if standby else "running")
        for unit in dict.fromkeys(units)
    ]
    for runner in settings["showrunners"]:
        if runner["session"] == session:
            runner["zone"] = zone
            known = {unit.session for unit in runner["units"]}
            runner["units"].extend(unit for unit in requested if unit.session not in known)
            return
    settings["showrunners"].append(Showrunner(session=session, zone=zone, units=requested))


def remove(settings: ShowrunnerSettings, session: str, units: list[str]) -> None:
    if not units:
        settings["showrunners"] = [runner for runner in settings["showrunners"] if runner["session"] != session]
    else:
        for runner in settings["showrunners"]:
            if runner["session"] == session:
                runner["units"] = [unit for unit in runner["units"] if unit.session not in units]


def ready(settings: ShowrunnerSettings, session: str, units: list[str]) -> list[str]:
    changed: set[str] = set()
    for runner in settings["showrunners"]:
        if runner["session"] == session:
            changed.update(unit.session for unit in runner["units"]
                           if isinstance(unit, StandingByUnitDirector) and unit.session in units)
            runner["units"] = [RunningUnitDirector(unit.session)
                               if isinstance(unit, StandingByUnitDirector) and unit.session in units
                               else unit for unit in runner["units"]]
            break
    return [unit for unit in units if unit not in changed]


def set_unit_status(settings: ShowrunnerSettings, showrunner_session: str,
                    state: RegisteredUnitDirector) -> bool:
    runners = [runner for runner in settings["showrunners"] if runner["session"] == showrunner_session]
    if not runners:
        raise ValueError(f"showrunner is absent: {showrunner_session}")
    if len(runners) != 1:
        raise ValueError(f"showrunner is ambiguous: {showrunner_session}")
    matches = [index for index, unit in enumerate(runners[0]["units"])
               if unit.session == state.session]
    if not matches:
        raise ValueError(f"unit is absent from {showrunner_session}: {state.session}")
    if len(matches) != 1:
        raise ValueError(f"unit is ambiguous in {showrunner_session}: {state.session}")
    index = matches[0]
    if type(runners[0]["units"][index]) is type(state):
        return False
    runners[0]["units"][index] = state
    return True


def stored_settings(settings: ShowrunnerSettings) -> dict[str, object]:
    runners: list[dict[str, object]] = []
    for runner in settings["showrunners"]:
        runners.append({"session": runner["session"], "zone": runner["zone"],
                        "units": [{"session": unit.session, "status": _unit_status(unit)}
                                  for unit in runner["units"]]})
    return {**settings, "showrunners": runners}


def _store(settings: ShowrunnerSettings) -> None:
    with tempfile.NamedTemporaryFile("w", dir=CONFIG.parent, prefix=".showrunners-", delete=False,
                                     encoding="utf-8") as temporary:
        json.dump(stored_settings(settings), temporary, indent=2)
        _ = temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, CONFIG)


def rewrite_prompt_names(old: str, new: str) -> None:
    status_command = re.compile(r"(unit_status\.sh\s+\S+\s+\S+\s+)([^|`\n]+)")
    for instance in NOTIFIER_STATE_DIR.glob("showrunner-*"):
        try:
            prompt_value = _fields(instance / "conf").get("PROMPT_FILE", "")
            prompt_path = Path(prompt_value)
            if not prompt_path.is_absolute():
                continue
            original = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue

        def replace_status(match: re.Match[str]) -> str:
            arguments = match.group(2)
            try:
                tokens = shlex.split(arguments)
            except ValueError:
                return match.group(0)
            if not tokens or (tokens[0] == "--showrunner" and len(tokens) != 2):
                return match.group(0)
            if old not in tokens:
                return match.group(0)
            renamed = [new if token == old else token for token in tokens]
            return match.group(1) + shlex.join(renamed) + arguments[len(arguments.rstrip()):]

        updated = status_command.sub(replace_status, original)
        if updated == original:
            continue
        with tempfile.NamedTemporaryFile("w", dir=prompt_path.parent, prefix=f".{prompt_path.name}-",
                                         delete=False, encoding="utf-8") as temporary:
            _ = temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, prompt_path.stat().st_mode)
        os.replace(temporary_path, prompt_path)


def change(action: str, session: str, zone: str, units: list[str], new_name: str = "",
           standby: bool = False) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        settings = load_settings() if CONFIG.exists() else defaults()
        if action == "add":
            add(settings, session, zone, units, standby)
        elif action == "remove":
            remove(settings, session, units)
        elif action == "ready":
            missing = ready(settings, session, units)
            for unit in missing:
                print(f"{unit} is not on standby")
            if len(missing) == len(units):
                return
        elif action == "rename":
            if not any(runner["session"] == session
                       or any(unit.session == session for unit in runner["units"])
                       for runner in settings["showrunners"]):
                return
            for runner in settings["showrunners"]:
                old_runner = runner["session"]
                if old_runner == session:
                    runner["session"] = new_name
                runner["units"] = [type(unit)(new_name if unit.session == session else unit.session)
                                   for unit in runner["units"]]
                result = subprocess.run([sys.executable, str(Path(__file__).with_name("stall_watch.py")),
                                         "rename-state", session, new_name, old_runner,
                                         runner["session"], *(unit.session for unit in runner["units"])],
                                        capture_output=True, text=True, check=False)
                if result.returncode != 0:
                    raise ValueError(f"stall state rename failed: {result.stderr.strip()}")
            rewrite_prompt_names(session, new_name)
        else:
            for runner in running_showrunners():
                if isinstance(runner.prompt, UnreadablePrompt):
                    print(f"showrunners: skipping showrunner-{runner.slug}: {runner.prompt.reason}", file=sys.stderr)
                    continue
                add(settings, runner.session, runner.prompt.zone, list(runner.prompt.unit_sessions))
        _store(settings)


def change_status(showrunner_session: str, state: RegisteredUnitDirector) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        settings = load_settings() if CONFIG.exists() else defaults()
        if set_unit_status(settings, showrunner_session, state):
            _store(settings)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    adding = commands.add_parser("add")
    _ = adding.add_argument("session")
    _ = adding.add_argument("--zone", required=True)
    _ = adding.add_argument("--unit", action="append", default=[])
    _ = adding.add_argument("--standby", action="store_true")
    removing = commands.add_parser("remove")
    _ = removing.add_argument("session")
    _ = removing.add_argument("--unit", action="append", default=[])
    making_ready = commands.add_parser("ready")
    _ = making_ready.add_argument("session")
    _ = making_ready.add_argument("--unit", action="append", required=True)
    renaming = commands.add_parser("rename")
    _ = renaming.add_argument("session")
    _ = renaming.add_argument("new_name")
    setting_status = commands.add_parser("status")
    _ = setting_status.add_argument("session")
    _ = setting_status.add_argument("--unit", required=True)
    _ = setting_status.add_argument("--state", required=True,
                                    choices=("running", "run-finished", "standing-by"))
    _ = commands.add_parser("import")
    _ = commands.add_parser("list")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    try:
        if action == "list":
            settings = load_settings()
            for runner in settings["showrunners"]:
                state = "running" if socket_for(runner["session"]) else "not running"
                unit_names = (f"{unit.session}:{_unit_status(unit)}" for unit in runner["units"])
                print(f"{runner['session']}\t{state}\t{runner['zone']}\t{' '.join(unit_names)}")
            for runner in missing_showrunners(settings):
                if isinstance(runner.prompt, PromptUnits):
                    detail = f"{runner.prompt.zone}\t{' '.join(runner.prompt.unit_sessions)}"
                else:
                    detail = f"<zone>\t<tmux session>\t{runner.prompt.reason}"
                print(f"missing: showrunner-{runner.slug}\t{runner.session}\t{detail}")
        elif action == "status":
            unit_session = cast(str, args.unit)
            change_status(cast(str, args.session), _unit_director(unit_session, cast(str, args.state)))
        else:
            change(action, cast(str, getattr(args, "session", "")), cast(str, getattr(args, "zone", "")),
                   cast(list[str], getattr(args, "unit", [])),
                   cast(str, getattr(args, "new_name", "")), cast(bool, getattr(args, "standby", False)))
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
