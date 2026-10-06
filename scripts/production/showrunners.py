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


class Showrunner(TypedDict):
    session: str
    zone: str
    units: list[str]


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


def defaults() -> ShowrunnerSettings:
    return ShowrunnerSettings(threshold_percent=2, repeat_minutes=30, stall_minutes=5,
                              faults_to="natedev", always=["natedev"], showrunners=[])


def load_settings(path: Path | None = None) -> ShowrunnerSettings:
    source = path or CONFIG
    try:
        raw = cast(object, json.loads(source.read_text(encoding="utf-8")))
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
            if (not isinstance(session, str) or not session or not isinstance(zone, str)
                    or not isinstance(units, list)
                    or not all(isinstance(unit, str) for unit in cast(list[object], units))):
                raise ValueError("invalid showrunner fields")
            _ = ZoneInfo(zone)
            checked.append(Showrunner(session=session, zone=zone, units=cast(list[str], units)))
        return ShowrunnerSettings(threshold_percent=cast(float, data["threshold_percent"]),
                                  repeat_minutes=cast(float, data["repeat_minutes"]),
                                  stall_minutes=cast(float, data["stall_minutes"]),
                                  faults_to=cast(str, data["faults_to"]), always=cast(list[str], always),
                                  showrunners=checked)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError(f"{source}: {error}") from error


def socket_for(target: str) -> str | None:
    result = subprocess.run([sys.executable, str(SESSIONS), "socket", target], capture_output=True,
                            text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _fields(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


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


def add(settings: ShowrunnerSettings, session: str, zone: str, units: list[str]) -> None:
    _ = ZoneInfo(zone)
    for runner in settings["showrunners"]:
        if runner["session"] == session:
            runner["zone"] = zone
            runner["units"].extend(unit for unit in units if unit not in runner["units"])
            return
    settings["showrunners"].append(Showrunner(session=session, zone=zone, units=list(dict.fromkeys(units))))


def remove(settings: ShowrunnerSettings, session: str, units: list[str]) -> None:
    if not units:
        settings["showrunners"] = [runner for runner in settings["showrunners"] if runner["session"] != session]
    else:
        for runner in settings["showrunners"]:
            if runner["session"] == session:
                runner["units"] = [unit for unit in runner["units"] if unit not in units]


def change(action: str, session: str, zone: str, units: list[str]) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        settings = load_settings() if CONFIG.exists() else defaults()
        if action == "add":
            add(settings, session, zone, units)
        elif action == "remove":
            remove(settings, session, units)
        else:
            for runner in running_showrunners():
                if isinstance(runner.prompt, UnreadablePrompt):
                    print(f"showrunners: skipping showrunner-{runner.slug}: {runner.prompt.reason}", file=sys.stderr)
                    continue
                add(settings, runner.session, runner.prompt.zone, list(runner.prompt.unit_sessions))
        with tempfile.NamedTemporaryFile("w", dir=CONFIG.parent, prefix=".showrunners-", delete=False,
                                         encoding="utf-8") as temporary:
            json.dump(settings, temporary, indent=2)
            _ = temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, CONFIG)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    adding = commands.add_parser("add")
    _ = adding.add_argument("session")
    _ = adding.add_argument("--zone", required=True)
    _ = adding.add_argument("--unit", action="append", default=[])
    removing = commands.add_parser("remove")
    _ = removing.add_argument("session")
    _ = removing.add_argument("--unit", action="append", default=[])
    _ = commands.add_parser("import")
    _ = commands.add_parser("list")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    try:
        if action == "list":
            settings = load_settings()
            for runner in settings["showrunners"]:
                state = "running" if socket_for(runner["session"]) else "not running"
                print(f"{runner['session']}\t{state}\t{runner['zone']}\t{' '.join(runner['units'])}")
            for runner in missing_showrunners(settings):
                if isinstance(runner.prompt, PromptUnits):
                    detail = f"{runner.prompt.zone}\t{' '.join(runner.prompt.unit_sessions)}"
                else:
                    detail = f"<zone>\t<tmux session>\t{runner.prompt.reason}"
                print(f"missing: showrunner-{runner.slug}\t{runner.session}\t{detail}")
        else:
            change(action, cast(str, getattr(args, "session", "")), cast(str, getattr(args, "zone", "")),
                   cast(list[str], getattr(args, "unit", [])))
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
