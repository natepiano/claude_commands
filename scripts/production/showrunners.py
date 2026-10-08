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
from typing import NamedTuple, NotRequired, TypedDict, cast
from zoneinfo import ZoneInfo

import unit_lookup

CONFIG = Path(os.environ.get("SHOWRUNNERS_CONFIG") or Path(__file__).resolve().parents[2] / "config/showrunners.json")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
SESSIONS = Path(os.environ.get("SHOWRUNNERS_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")


class Showrunner(TypedDict):
    session: str
    zone: str
    # The production doc, whose Units table says which units the showrunner has. Empty until the
    # showrunner registers; its units are then unknown, not none.
    doc: str
    # The unit list an entry held before units were looked up. Nothing here reads it. It is kept
    # through every rewrite, because `adopt.py` takes each unit's run state from it, and one
    # showrunner's write must not lose the states of a showrunner that has not adopted yet.
    units: NotRequired[list[object]]


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
    prompt: PromptZone | UnreadablePrompt
    doc: CheckedDoc | NoCheckedDoc


class PromptZone(NamedTuple):
    zone: str


class UnreadablePrompt(NamedTuple):
    reason: str


class CheckedDoc(NamedTuple):
    path: Path


class NoCheckedDoc(NamedTuple):
    reason: str


def defaults() -> ShowrunnerSettings:
    return ShowrunnerSettings(threshold_percent=2, repeat_minutes=30, stall_minutes=5,
                              faults_to="natedev", always=["natedev"], showrunners=[])


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
            session, zone, doc = entry.get("session"), entry.get("zone"), entry.get("doc", "")
            if (not isinstance(session, str) or not session or not isinstance(zone, str)
                    or not isinstance(doc, str)):
                raise ValueError("invalid showrunner fields")
            _ = ZoneInfo(zone)
            runner = Showrunner(session=session, zone=zone, doc=doc)
            units = entry.get("units")
            if isinstance(units, list):
                runner["units"] = cast(list[object], units)
            checked.append(runner)
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


def _prompt_zone(fields: dict[str, str]) -> PromptZone | UnreadablePrompt:
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
    match = re.search(r"unit_status\.sh\s+\S+\s+([^\s|`]+)", prompt)
    if match is None:
        return UnreadablePrompt("unit_status.sh command is missing its zone")
    zone = match.group(1)
    try:
        _ = ZoneInfo(zone)
    except (KeyError, ValueError) as error:
        return UnreadablePrompt(f"prompt has an invalid zone: {error}")
    return PromptZone(zone)


def _running(instance: Path) -> RunningShowrunner | None:
    fields = _fields(instance / "conf")
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    socket = socket_for(target)
    if socket is None:
        return None
    return RunningShowrunner(instance.name.removeprefix("showrunner-"), _session_name(target[8:]),
                             socket, _prompt_zone(fields), checked_doc(instance))


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


def add(settings: ShowrunnerSettings, session: str, zone: str, doc: str) -> None:
    """Register the showrunner, or bring its zone and doc up to date. An empty doc keeps the one held."""
    _ = ZoneInfo(zone)
    for runner in settings["showrunners"]:
        if runner["session"] == session:
            runner["zone"] = zone
            runner["doc"] = doc or runner["doc"]
            return
    settings["showrunners"].append(Showrunner(session=session, zone=zone, doc=doc))


def remove(settings: ShowrunnerSettings, session: str) -> None:
    settings["showrunners"] = [runner for runner in settings["showrunners"] if runner["session"] != session]


def production_slug(doc: str) -> str:
    """The slug a production's units are marked with: its doc's name without the suffix."""
    slug = Path(doc).name.removesuffix("-production.md")
    if not doc or slug == Path(doc).name:
        raise ValueError(f"not a production doc: {doc or 'none registered'}")
    return slug


def ready(session: str, units: list[str]) -> None:
    """Mark each standing-by unit as running. Its state is a mark on its own tmux session."""
    runner = next((item for item in load_settings()["showrunners"] if item["session"] == session), None)
    if runner is None:
        raise ValueError(f"showrunner is absent: {session}")
    marked = unit_lookup.marked_units(production_slug(runner["doc"]))
    for unit in units:
        found = marked.get(unit)
        if found is None or found.state is not unit_lookup.UnitState.STANDING_BY:
            print(f"{unit} is not on standby")
            continue
        unit_lookup.set_state(found.pane, unit_lookup.UnitState.RUNNING)


def set_own_state(state: str) -> None:
    """Record the calling unit's run state on the tmux session it runs in."""
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        raise ValueError("status is set by a unit from its own tmux pane; this is not one")
    unit_lookup.set_state(pane, unit_lookup.UnitState(state))


def stored_settings(settings: ShowrunnerSettings) -> dict[str, object]:
    runners: list[dict[str, object]] = []
    for runner in settings["showrunners"]:
        runners.append({"session": runner["session"], "zone": runner["zone"], "doc": runner["doc"],
                        **({"units": runner["units"]} if "units" in runner else {})})
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


def change(action: str, session: str, zone: str, doc: str, new_name: str = "") -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        settings = load_settings() if CONFIG.exists() else defaults()
        if action == "add":
            add(settings, session, zone, doc)
        elif action == "remove":
            remove(settings, session)
        elif action == "rename":
            if not any(runner["session"] == session for runner in settings["showrunners"]):
                return
            for runner in settings["showrunners"]:
                if runner["session"] == session:
                    runner["session"] = new_name
            rewrite_prompt_names(session, new_name)
        else:
            for runner in running_showrunners():
                if isinstance(runner.prompt, UnreadablePrompt):
                    print(f"showrunners: skipping showrunner-{runner.slug}: {runner.prompt.reason}", file=sys.stderr)
                    continue
                add(settings, runner.session, runner.prompt.zone,
                    str(runner.doc.path) if isinstance(runner.doc, CheckedDoc) else "")
        _store(settings)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    adding = commands.add_parser("add")
    _ = adding.add_argument("session")
    _ = adding.add_argument("--zone", required=True)
    _ = adding.add_argument("--doc", default="")
    removing = commands.add_parser("remove")
    _ = removing.add_argument("session")
    making_ready = commands.add_parser("ready")
    _ = making_ready.add_argument("session")
    _ = making_ready.add_argument("--unit", action="append", required=True)
    renaming = commands.add_parser("rename")
    _ = renaming.add_argument("session")
    _ = renaming.add_argument("new_name")
    setting_status = commands.add_parser("status")
    # A unit that started before its state became a mark on its own tmux session still names the
    # showrunner and itself here. Both are accepted and unused: the pane says which unit calls.
    _ = setting_status.add_argument("session", nargs="?")
    _ = setting_status.add_argument("--unit")
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
                print(f"{runner['session']}\t{state}\t{runner['zone']}\t{runner['doc'] or '<no doc registered>'}")
            for runner in missing_showrunners(settings):
                zone = runner.prompt.zone if isinstance(runner.prompt, PromptZone) else f"<zone>\t{runner.prompt.reason}"
                doc = str(runner.doc.path) if isinstance(runner.doc, CheckedDoc) else f"<doc>\t{runner.doc.reason}"
                print(f"missing: showrunner-{runner.slug}\t{runner.session}\t{zone}\t{doc}")
        elif action == "status":
            set_own_state(cast(str, args.state))
        elif action == "ready":
            ready(cast(str, args.session), cast(list[str], args.unit))
        else:
            change(action, cast(str, getattr(args, "session", "")), cast(str, getattr(args, "zone", "")),
                   cast(str, getattr(args, "doc", "")), cast(str, getattr(args, "new_name", "")))
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
