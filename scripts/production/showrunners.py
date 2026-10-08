#!/usr/bin/env python3
"""Read this machine's showrunners from their update timers, and its showrunner settings."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

# The alert tools load this file as part of a package, where its own directory is not on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import unit_lookup  # noqa: E402

CONFIG = Path(os.environ.get("SHOWRUNNERS_CONFIG") or Path(__file__).resolve().parents[2] / "config/showrunners.json")
NOTIFIER_STATE_DIR = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier")
SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
SESSIONS = Path(os.environ.get("SHOWRUNNERS_SESSIONS") or Path(__file__).resolve().parent.parent / "message/sessions.py")


class Showrunner(TypedDict):
    """One showrunner, read from its update timer each time. Nothing here is stored in the config."""

    # The session's name now, looked up by its Claude session id. Empty while it is not running.
    session: str
    # Where a message reaches it now. Empty while it is not running.
    socket: str
    # The production's slug, which names the showrunner whatever its session is called.
    slug: str
    zone: str
    # The production doc, whose Units table says which units the showrunner has. Empty until the
    # showrunner registers; its units are then unknown, not none.
    doc: str


class ShowrunnerSettings(TypedDict):
    threshold_percent: float
    repeat_minutes: float
    stall_minutes: float
    faults_to: str
    always: list[str]


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
                              faults_to="natedev", always=["natedev"])


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
        always = data.get("always")
        if not isinstance(always, list) or not all(isinstance(item, str) for item in cast(list[object], always)):
            raise ValueError("invalid always")
        # A file written while showrunners were listed here still has the list; it is not read.
        return ShowrunnerSettings(threshold_percent=cast(float, data["threshold_percent"]),
                                  repeat_minutes=cast(float, data["repeat_minutes"]),
                                  stall_minutes=cast(float, data["stall_minutes"]),
                                  faults_to=cast(str, data["faults_to"]), always=cast(list[str], always))
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
            try:
                os.kill(pid, 0)
            except PermissionError:
                # Another user's process is still a live one.
                pass
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


def _registered(instance: Path) -> Showrunner:
    fields = _fields(instance / "conf")
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    prompt = _prompt_zone(fields)
    if isinstance(prompt, UnreadablePrompt):
        raise ValueError(prompt.reason)
    doc = checked_doc(instance)
    socket = socket_for(target)
    name = _session_name(target[8:]) if socket is not None else ""
    return Showrunner(session=name, socket=socket or "", slug=instance.name.removeprefix("showrunner-"), zone=prompt.zone,
                      doc=str(doc.path) if isinstance(doc, CheckedDoc) else "")


def registered_showrunners(state_dir: Path | None = None) -> list[Showrunner]:
    """Every showrunner that has an update timer, running or not.

    The timer is addressed to the Claude session id and holds the production doc, so it is the one
    record of a showrunner and holds no name: the name is looked up here, at each call. Raises OSError
    when the session records cannot say whether a showrunner runs; a timer that is not a complete
    showrunner's is left out, with the reason on stderr.
    """
    found: list[Showrunner] = []
    for instance in sorted((state_dir or NOTIFIER_STATE_DIR).glob("showrunner-*")):
        if not instance.is_dir():
            continue
        try:
            found.append(_registered(instance))
        except (KeyError, ValueError, UnicodeError) as error:
            print(f"showrunners: skipping {instance.name}: {error}", file=sys.stderr)
    return found


def production_slug(doc: str) -> str:
    """The slug a production's units are marked with: its doc's name without the suffix."""
    slug = Path(doc).name.removesuffix("-production.md")
    if not doc or slug == Path(doc).name:
        raise ValueError(f"not a production doc: {doc or 'none registered'}")
    return slug


def current_name(slug: str) -> str:
    """The name now of the production's showrunner. Empty while it is not running or has no update timer."""
    return next((runner["session"] for runner in registered_showrunners() if runner["slug"] == slug), "")


def named(showrunner: str) -> Showrunner:
    """The showrunner called `showrunner` now, or whose production has that slug."""
    for runner in registered_showrunners():
        if showrunner and showrunner in (runner["session"], runner["slug"]):
            return runner
    raise ValueError(f"no showrunner with an update timer is called {showrunner} or runs that production")


def ready(session: str, units: list[str]) -> None:
    """Mark each standing-by unit as running. Its state is a mark on its own tmux session."""
    runner = named(session)
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    making_ready = commands.add_parser("ready")
    _ = making_ready.add_argument("session")
    _ = making_ready.add_argument("--unit", action="append", required=True)
    naming = commands.add_parser("name")
    _ = naming.add_argument("slug")
    setting_status = commands.add_parser("status")
    # A unit that started before its state became a mark on its own tmux session still names the
    # showrunner and itself here. Both are accepted and unused: the pane says which unit calls.
    _ = setting_status.add_argument("session", nargs="?")
    _ = setting_status.add_argument("--unit")
    _ = setting_status.add_argument("--state", required=True,
                                    choices=("running", "run-finished", "standing-by"))
    _ = commands.add_parser("list")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    try:
        if action == "list":
            for runner in registered_showrunners():
                print(f"{runner['slug']}\t{runner['session'] or '<not running>'}\t{runner['zone']}"
                      + f"\t{runner['doc'] or '<no doc in its check>'}")
        elif action == "name":
            name = current_name(cast(str, args.slug))
            if not name:
                raise ValueError(f"the showrunner of {cast(str, args.slug)} is not running")
            print(name)
        elif action == "status":
            set_own_state(cast(str, args.state))
        elif action == "ready":
            ready(cast(str, args.session), cast(list[str], args.unit))
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
