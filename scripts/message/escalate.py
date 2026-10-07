#!/usr/bin/env python3
"""Hold news for the user, and send it when they have not typed in a terminal in time.

  escalate.py hold KEY --summary TITLE --text TEXT [--minutes 15] [--need note|decision|blocked]
  escalate.py close KEY    the news is over; the next hold of KEY starts a new wait
  escalate.py typed        the user typed in a terminal: the prompt hook's call
  escalate.py due          settle each waiting message: the notifier job's call
  escalate.py list         each held message and where it stands

`hold` is for news a terminal already shows. It waits `--minutes`: if the user
types in any terminal in that time it is never sent, and if not, `due` sends it
with `send.py --to user`. Holding a KEY already held changes nothing, so a caller
may hold on every check and `close` once the news stops being true.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, TypedDict, cast

if TYPE_CHECKING:
    from send import Need

STATE = Path(os.environ.get("ESCALATE_STATE_DIR")
             or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "escalate")
SEND = Path(os.environ.get("ESCALATE_SEND") or Path(__file__).with_name("send.py"))
NOTIFIER = Path(os.environ.get("ESCALATE_NOTIFIER") or Path(__file__).with_name("notifier.sh"))
PY = Path(__file__).resolve().parent.parent / "lib" / "py"
JOB = "escalate"
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
USAGE = ("usage: escalate.py hold KEY --summary TITLE --text TEXT [--minutes N] [--need note|decision|blocked]"
         + " | close KEY | typed | due | list")

Outcome = Literal["waiting", "sent", "answered"]


class Held(TypedDict):
    summary: str
    text: str
    need: Need
    held_at: float
    minutes: int
    outcome: Outcome


class Settled(NamedTuple):
    line: str
    failed: bool


@contextmanager
def locked() -> Generator[None]:
    """One writer at a time, so a close never loses to a `due` settling the same key."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def held_path(key: str) -> Path:
    return STATE / "held" / f"{key}.json"


def read(path: Path) -> Held | None:
    try:
        return cast(Held, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def write(path: Path, held: Held) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    _ = scratch.write_text(json.dumps(held), encoding="utf-8")
    _ = scratch.replace(path)


def typed() -> None:
    """Record that the user typed in a terminal just now."""
    STATE.mkdir(parents=True, exist_ok=True)
    _ = (STATE / "typed").write_text(repr(time.time()), encoding="utf-8")


def typed_at() -> float:
    try:
        return float((STATE / "typed").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0


def schedule() -> None:
    """Have the notifier run `due` every minute; a repeated `new` changes nothing."""
    _ = subprocess.run(["zsh", str(NOTIFIER), "new", JOB, "--every", "1",
                        "--run", f"{PY} {Path(__file__).resolve()} due"], capture_output=True, text=True, check=True)


def hold(key: str, summary: str, text: str, minutes: int = 15, need: Need = "decision") -> bool:
    """Start KEY's wait; false when KEY is already held."""
    with locked():
        path = held_path(key)
        if path.exists():
            return False
        schedule()
        write(path, {"summary": summary, "text": text, "need": need, "held_at": time.time(), "minutes": minutes,
                     "outcome": "waiting"})
    return True


def close(key: str) -> bool:
    """Forget KEY; false when it was not held."""
    with locked():
        try:
            held_path(key).unlink()
        except FileNotFoundError:
            return False
    return True


def deliver(held: Held) -> str:
    """Send one held message to the user; the empty string when sent, else send.py's own line."""
    done = subprocess.run([sys.executable, str(SEND), "--to", "user", "--from", JOB, "--summary", held["summary"],
                           "--need", held["need"], "--text", held["text"]], capture_output=True, text=True, check=False)
    return "" if done.returncode == 0 else " ".join((done.stdout or done.stderr).split()) or f"exit {done.returncode}"


def due(now: float) -> list[Settled]:
    """Settle each waiting message: answered when the user typed since it was held, sent once its time is up."""
    settled: list[Settled] = []
    with locked():
        seen = typed_at()
        for path in sorted((STATE / "held").glob("*.json")):
            held = read(path)
            if held is None or held["outcome"] != "waiting":
                continue
            if seen >= held["held_at"]:
                held["outcome"], line = "answered", "the user typed in a terminal; not sent"
            elif now < held["held_at"] + held["minutes"] * 60:
                continue
            elif problem := deliver(held):
                settled.append(Settled(f"{path.stem}: NOT sent: {problem}", True))
                continue
            else:
                held["outcome"], line = "sent", "sent to the user"
            write(path, held)
            settled.append(Settled(f"{path.stem}: {line}", False))
    return settled


def listing(now: float) -> list[str]:
    lines: list[str] = []
    for path in sorted((STATE / "held").glob("*.json")):
        held = read(path)
        if held is None:
            continue
        left = max(math.ceil((held["held_at"] + held["minutes"] * 60 - now) / 60), 0)
        lines.append(f"{path.stem} - " + (f"waiting, sent in {left} min" if held["outcome"] == "waiting"
                                          else held["outcome"]) + f" - {held['summary']}")
    return lines or ["nothing held"]


def main(arguments: list[str]) -> int:
    match arguments:
        case ["hold", key, *pairs] if KEY.fullmatch(key) and len(pairs) % 2 == 0:
            from send import CHANNEL_PRIORITY

            options = {"--minutes": "15", "--need": "decision", **dict(zip(pairs[::2], pairs[1::2]))}
            if options.keys() != {"--summary", "--text", "--minutes", "--need"} \
                    or options["--need"] not in CHANNEL_PRIORITY or not options["--minutes"].isdigit():
                print(USAGE, file=sys.stderr)
                return 2
            started = hold(key, options["--summary"], options["--text"], int(options["--minutes"]),
                           options["--need"])
            print(f"{key}: held; sent to the user in {options['--minutes']} min unless they type in a terminal"
                  if started else f"{key}: already held")
        case ["close", key] if KEY.fullmatch(key):
            print(f"{key}: closed" if close(key) else f"{key}: not held")
        case ["typed"]:
            typed()
        case ["due"]:
            settled = due(time.time())
            for one in settled:
                print(one.line)
            return 1 if any(one.failed for one in settled) else 0
        case ["list"]:
            print("\n".join(listing(time.time())))
        case _:
            print(USAGE, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
