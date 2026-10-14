#!/usr/bin/env python3
"""Read or send input to a production unit or live Claude session on either host."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / "herdr"))
sys.path.insert(0, str(SCRIPTS / "message"))
import herdr  # noqa: E402
from herdr import AgentBlocked, Herdr, HerdrPlace  # noqa: E402
from herdr import HerdrFailure as HerdrFailure  # noqa: E402
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session  # noqa: E402
from unit_lookup import Place, TmuxPlace, marked_units, sessions_dir, tmux_binary  # noqa: E402

USAGE = ("usage: session_pane.py <read|type|keys|where> <unit <slug> <unit>|session <id>> "
         "[--lines N --joined --ansi | <text> | <key>...]")


class TmuxFailure(OSError):
    """The tmux host could not complete the requested pane operation."""


class NoSuchTarget(Exception):
    """No live session or marked unit identifies the requested pane."""


def _tmux(*arguments: str) -> str:
    try:
        result = subprocess.run([tmux_binary(), *arguments], capture_output=True, text=True,
                                check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise TmuxFailure(f"tmux: {error}") from error
    if result.returncode:
        raise TmuxFailure(f"tmux: {result.stderr.strip() or f'exit {result.returncode}'}")
    return result.stdout


def _trim_tmux_output(text: str, lines: int, *, ansi: bool) -> str:
    """Discard screen padding before retaining the requested written rows."""
    rows = text.split("\n")
    while rows:
        last = rows[-1]
        if ansi:
            # CSI, OSC and other ESC sequences affect presentation, not row content.
            last = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[@-~])", "", last)
        if last.strip():
            break
        _ = rows.pop()
    return "\n".join(rows[-lines:]) + "\n" if rows else ""


def read_place(place: Place, lines: int = 60, *, joined: bool = False, ansi: bool = False) -> str:
    if lines <= 0:
        raise ValueError("lines must be positive")
    if isinstance(place, HerdrPlace):
        return Herdr(place.session).read(place.pane, lines, joined=joined, ansi=ansi)
    options = ["capture-pane", "-p"]
    if joined:
        options.append("-J")
    if ansi:
        options.append("-e")
    captured = _tmux(*options, "-S", f"-{lines}", "-t", place.pane)
    return _trim_tmux_output(captured, lines, ansi=ansi)


def type_place(place: Place, text: str) -> None:
    if isinstance(place, HerdrPlace):
        Herdr(place.session).prompt(place.pane, text)
    else:
        _ = _tmux("send-keys", "-t", place.pane, "-l", text)
        _ = _tmux("send-keys", "-t", place.pane, "Enter")


def keys_place(place: Place, *keys: str) -> None:
    if isinstance(place, HerdrPlace):
        Herdr(place.session).send_keys(place.pane, *keys)
    else:
        _ = _tmux("send-keys", "-t", place.pane, *keys)


def where_place(place: Place) -> str:
    return (f"herdr:{place.session}:{place.pane}" if isinstance(place, HerdrPlace)
            else f"tmux:{place.pane}")


def _session_environment(pid: int) -> dict[str, str]:
    process = Path(os.environ.get("HERDR_PROC", "/proc")) / str(pid)
    entries = process.joinpath("environ").read_bytes().decode(errors="replace").split("\0")
    return dict(entry.split("=", 1) for entry in entries if "=" in entry)


def session_place(session_id: str) -> Place:
    unread = False
    records: list[SessionRecord] = []
    for path in sessions_dir().iterdir():
        if path.suffix != ".json":
            continue
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord):
            unread = True
        elif record["sessionId"] == session_id and live_session(record):
            records.append(record)
    if not records:
        if unread:
            raise OSError("one or more session records could not be read")
        raise NoSuchTarget(session_id)
    record = max(records, key=lambda record: record["updatedAt"])
    if sys.platform == "darwin":
        # ps combines argv and environment without a reliable boundary. The live
        # registry's pane id stays true when its recorded tmux session name changes.
        pane = record["tmux"].rpartition(".")[2]
        if re.fullmatch(r"%[0-9]+", pane):
            return TmuxPlace(pane)
    else:
        environment = _session_environment(record["pid"])
        if pane := environment.get("TMUX_PANE", ""):
            return TmuxPlace(pane)
    place = herdr.herdr_place_of(record["pid"])
    if isinstance(place, herdr.NotInHerdr):
        raise NoSuchTarget(session_id)
    return place


def main(arguments: list[str]) -> int:
    target = ""
    try:
        if len(arguments) < 3 or arguments[0] not in ("read", "type", "keys", "where"):
            raise ValueError(USAGE)
        verb, kind = arguments[:2]
        if kind == "unit" and len(arguments) >= 4:
            slug, name = arguments[2:4]
            target, rest = f"unit {slug} {name}", arguments[4:]
        elif kind == "session":
            name = arguments[2]
            target, rest = f"session {name}", arguments[3:]
            slug = ""
        else:
            raise ValueError(USAGE)
        lines, joined, ansi = 60, False, False
        if verb == "read":
            while rest:
                option, rest = rest[0], rest[1:]
                if option == "--lines" and rest:
                    lines, rest = int(rest[0]), rest[1:]
                    if lines <= 0:
                        raise ValueError("lines must be positive")
                elif option == "--joined":
                    joined = True
                elif option == "--ansi":
                    ansi = True
                else:
                    raise ValueError(USAGE)
        elif (verb == "type" and len(rest) != 1 or verb == "keys" and not rest
              or verb == "where" and rest):
            raise ValueError(USAGE)
        if kind == "unit":
            unit = marked_units(slug).get(name)
            if unit is None:
                raise NoSuchTarget(target)
            place = unit.place
        else:
            place = session_place(name)
        if verb == "read":
            print(read_place(place, lines, joined=joined, ansi=ansi), end="")
        elif verb == "type":
            type_place(place, rest[0])
        elif verb == "keys":
            keys_place(place, *rest)
        else:
            print(where_place(place))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    except NoSuchTarget:
        print(f"{target}: no such target", file=sys.stderr)
        return 1
    except AgentBlocked:
        print(f"{target}: blocked at a prompt; nothing typed", file=sys.stderr)
        return 4
    except (OSError, HerdrFailure) as error:
        print(f"{target}: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
