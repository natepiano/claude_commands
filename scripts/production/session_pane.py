#!/usr/bin/env python3
"""Read or send input to a production unit or live Claude session in herdr."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import math
import sys

from scripts.herdr import herdr  # noqa: E402
from scripts.herdr.herdr import AgentBlocked, Herdr  # noqa: E402
from scripts.herdr.herdr import HerdrFailure as HerdrFailure  # noqa: E402
from scripts.message.sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session  # noqa: E402
from scripts.production.unit_lookup import LiveSession, LiveClaude, Place, labeled_session, sessions_dir  # noqa: E402
from scripts.lib.script_envelope import script_prompt  # noqa: E402

USAGE = ("usage: session_pane.py <read|type|keys|where> <unit <production_name> <unit>|session <id>> "
         "[--lines N --joined --ansi | <text> | <key>...]")


class NoSuchTarget(Exception):
    """No live session or unit with session labels identifies the requested pane."""


def read_place(place: Place, lines: int = 60, *, joined: bool = False, ansi: bool = False,
               deadline: float = math.inf) -> str:
    if lines <= 0:
        raise ValueError("lines must be positive")
    return Herdr(place.session).read(place.pane, lines, joined=joined, ansi=ansi, deadline=deadline)


def agent_status(place: Place, *, deadline: float = math.inf) -> herdr.AgentStatus:
    """herdr's state for the agent in the place's pane, as a typed state; unknown when no agent is present."""
    return next((agent["agent_status"] for agent in Herdr(place.session).agents(deadline=deadline)
                 if agent["pane_id"] == place.pane), herdr.AgentStatus.UNKNOWN)


def type_place(place: Place, text: str, *, target: str) -> None:
    Herdr(place.session).prompt(place.pane, script_prompt(text), target=target)


def keys_place(place: Place, *keys: str, target: str) -> None:
    Herdr(place.session).send_keys(place.pane, *keys, target=target)


def where_place(place: Place) -> str:
    return f"herdr:{place.session}:{place.pane}"


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
    return record_place(max(records, key=lambda record: record["updatedAt"]))


def record_place(record: SessionRecord) -> Place:
    """Resolve the pane owned by this specific registry process."""
    place = herdr.herdr_place_of(record["pid"])
    if isinstance(place, herdr.NotInHerdr):
        raise NoSuchTarget(record["sessionId"])
    return place


def main(arguments: list[str]) -> int:
    target = ""
    try:
        if len(arguments) < 3 or arguments[0] not in ("read", "type", "keys", "where"):
            raise ValueError(USAGE)
        verb, kind = arguments[:2]
        if kind == "unit" and len(arguments) >= 4:
            production_name, name = arguments[2:4]
            target, rest = f"unit {production_name} {name}", arguments[4:]
        elif kind == "session":
            name = arguments[2]
            target, rest = f"session {name}", arguments[3:]
            production_name = ""
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
            unit = labeled_session(production_name, name)
            if unit is None:
                raise NoSuchTarget(target)
            place = unit.place
            session_id = unit.claude.session_id if isinstance(unit.claude, LiveClaude) else unit.identity.id if isinstance(unit.identity, LiveSession) else ""
        else:
            place = session_place(name)
            session_id = name
        if verb == "read":
            print(read_place(place, lines, joined=joined, ansi=ansi), end="")
        elif verb == "type":
            type_place(place, rest[0], target=session_id)
        elif verb == "keys":
            keys_place(place, *rest, target=session_id)
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
