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
    # The user's zone, read from the production doc.
    zone: str
    # The production doc, whose Units table says which units the showrunner has.
    doc: str


class ShowrunnerSettings(TypedDict):
    threshold_percent: float
    repeat_minutes: float
    stall_minutes: float
    faults_to: str
    always: list[str]


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


def _doc_zone(doc: Path) -> str:
    """The user's zone, from the production doc's own User zone line."""
    prefix = "- **User zone:** "
    try:
        lines = doc.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError(f"production doc could not be read: {error}") from error
    found = next((re.search(r"[A-Za-z][\w+-]*(?:/[\w+-]+)*", line[len(prefix):]) for line in lines
                  if line.startswith(prefix)), None)
    if found is None:
        raise ValueError("production doc lacks User zone")
    try:
        _ = ZoneInfo(found.group(0))
    except (KeyError, ValueError) as error:
        raise ValueError(f"production doc has an invalid User zone: {error}") from error
    return found.group(0)


def _registered(instance: Path) -> Showrunner:
    fields = _fields(instance / "conf")
    target = fields["TARGET"]
    if not target.startswith("session:") or not target[8:]:
        raise ValueError("invalid TARGET")
    doc = checked_doc(instance)
    if isinstance(doc, NoCheckedDoc):
        raise ValueError(doc.reason)
    zone = _doc_zone(doc.path)
    socket = socket_for(target)
    name = _session_name(target[8:]) if socket is not None else ""
    return Showrunner(session=name, socket=socket or "", slug=instance.name.removeprefix("showrunner-"), zone=zone,
                      doc=str(doc.path))


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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    making_ready = commands.add_parser("ready")
    _ = making_ready.add_argument("session", nargs="?")
    _ = making_ready.add_argument("--unit", action="append")
    naming = commands.add_parser("name")
    _ = naming.add_argument("slug")
    # Does nothing: a unit's run state is read from its run records. Delete once no session started before that is left.
    setting_status = commands.add_parser("status")
    _ = setting_status.add_argument("session", nargs="?")
    _ = setting_status.add_argument("--unit")
    _ = setting_status.add_argument("--state")
    _ = commands.add_parser("list")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    try:
        if action == "list":
            for runner in registered_showrunners():
                print(f"{runner['slug']}\t{runner['session'] or '<not running>'}\t{runner['zone']}"
                      + f"\t{runner['doc']}")
        elif action == "name":
            name = current_name(cast(str, args.slug))
            if not name:
                raise ValueError(f"the showrunner of {cast(str, args.slug)} is not running")
            print(name)
    except (OSError, ValueError, KeyError) as error:
        print(f"showrunners: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
