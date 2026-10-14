#!/usr/bin/env python3
"""A stand-in tmux for tests: sessions, panes and marks come from a JSON file.

`FAKE_TMUX_STATE` names the file: {"<session id>": {"label": str, "panes": [str], "env": {str: str}}}.
A session may also hold `screens` ({pane: text}, what `capture-pane` prints) and `sent` (each
`send-keys` call's pane and keys, appended). It answers the calls `unit_lookup.py` and
`unit_effort.py` make and nothing else. A missing file is a tmux with no server.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NotRequired, TypedDict, cast


class FakeSession(TypedDict):
    label: str
    panes: list[str]
    env: dict[str, str]
    screens: NotRequired[dict[str, str]]
    sent: NotRequired[list[list[str]]]


def write(path: Path, sessions: dict[str, FakeSession]) -> None:
    _ = path.write_text(json.dumps(sessions), encoding="utf-8")


def read(path: Path) -> dict[str, FakeSession]:
    return cast(dict[str, FakeSession], json.loads(path.read_text(encoding="utf-8")))


def _find(sessions: dict[str, FakeSession], target: str) -> FakeSession | None:
    for key, session in sessions.items():
        if target in (key, session["label"], f"={session['label']}", *session["panes"]):
            return session
    return None


def main(arguments: list[str]) -> int:
    path = Path(os.environ["FAKE_TMUX_STATE"])
    if not path.exists():
        print("no server running on /tmp/fake", file=sys.stderr)
        return 1
    sessions = read(path)
    match arguments:
        case ["list-panes", "-a", "-F", _]:
            for key, session in sessions.items():
                for pane in session["panes"]:
                    print(f"{key}\t{pane}\t{session['label']}")
            return 0
        case ["show-environment", "-t", target]:
            found = _find(sessions, target)
            if found is None:
                print(f"can't find session: {target}", file=sys.stderr)
                return 1
            for name, value in found["env"].items():
                print(f"{name}={value}")
            return 0
        case ["set-environment", "-t", target, name, value]:
            found = _find(sessions, target)
            if found is None:
                print(f"can't find session: {target}", file=sys.stderr)
                return 1
            found["env"][name] = value
            write(path, sessions)
            return 0
        case ["set-environment", "-u", "-t", target, name]:
            found = _find(sessions, target)
            if found is None:
                print(f"can't find session: {target}", file=sys.stderr)
                return 1
            _ = found["env"].pop(name, None)
            write(path, sessions)
            return 0
        case ["capture-pane", "-p", "-e", "-t", pane]:
            found = _find(sessions, pane)
            if found is None:
                print(f"can't find pane: {pane}", file=sys.stderr)
                return 1
            print(found.get("screens", {}).get(pane, ""))
            return 0
        case ["send-keys", "-t", pane, *keys]:
            found = _find(sessions, pane)
            if found is None:
                print(f"can't find pane: {pane}", file=sys.stderr)
                return 1
            found.setdefault("sent", []).append([pane, *keys])
            write(path, sessions)
            return 0
        case _:
            print(f"fake tmux: unexpected call: {arguments}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
