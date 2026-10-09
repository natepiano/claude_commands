#!/usr/bin/env python3
"""Send every showrunner, unit director or other agent on this machine a message at once.

  broadcast.py --from NAME (--all TEXT | --showrunners TEXT | --units TEXT | --agents TEXT)...

Every recipient gets its own short-lived `send.py`, all started together, so a
broadcast takes as long as its slowest delivery. `--all` gives every role one
text, and a role's own flag after it gives that role its own version. Other
agents are every other live Claude session, seats included, and every running
Codex seat. Each message ends with who was sent it, so nobody passes it on. The
sender is never a recipient. One session, or the user, is `send.py --to NAME`.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, NamedTuple

import showrunners
from live_units import units_of

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
from sessions import SessionRecord, UnreadableSessionRecord, live_session, read_session  # noqa: E402

SEND = Path(os.environ.get("BROADCAST_SEND") or MESSAGE / "send.py")
PS = shlex.split(os.environ.get("BROADCAST_PS") or "ps -eo pid=,ppid=,comm=,args=")
USAGE = "usage: broadcast.py --from <your session> (--all TEXT | --showrunners TEXT | --units TEXT | --agents TEXT)..."
# One relay peaked at 272 MB when measured (2026-10-07), so this many together hold about 6.5 GB.
AT_ONCE = 24

Role = Literal["showrunner", "unit director", "agent"]
Outcome = Literal["sent", "no live session", "NOT sent"]

SAID: dict[Role, str] = {"showrunner": "showrunner", "unit director": "unit director", "agent": "other agent"}
FLAGS: dict[str, tuple[Role, ...]] = {"--all": tuple(SAID), "--showrunners": ("showrunner",),
                                      "--units": ("unit director",), "--agents": ("agent",)}


class Recipient(NamedTuple):
    session: str
    role: Role
    # A Codex seat's session directory; empty for a Claude session.
    codex: str = ""


class Delivery(NamedTuple):
    recipient: Recipient
    outcome: Outcome
    detail: str


def live_sessions() -> dict[str, SessionRecord]:
    """Each live named session, the newest record winning a shared name."""
    newest: dict[str, SessionRecord] = {}
    for path in showrunners.SESSIONS_DIR.glob("*.json"):
        record = read_session(path)
        if isinstance(record, UnreadableSessionRecord) or not record["name"] or not live_session(record):
            continue
        if record["name"] not in newest or record["updatedAt"] > newest[record["name"]]["updatedAt"]:
            newest[record["name"]] = record
    return newest


class Process(NamedTuple):
    pid: int
    parent: int
    command: str
    arguments: str


def processes() -> dict[int, Process]:
    listing = subprocess.run(PS, capture_output=True, text=True, check=True)
    table: dict[int, Process] = {}
    for line in listing.stdout.splitlines():
        fields = line.split(None, 3)
        if len(fields) >= 3:
            table[int(fields[0])] = Process(int(fields[0]), int(fields[1]), fields[2],
                                            fields[3] if len(fields) > 3 else "")
    return table


def is_codex_agent(process: Process) -> bool:
    words = process.arguments.split()
    if process.command == "codex":
        return words[1:2] == ["exec"]
    if not process.command.startswith("python"):
        return False
    mesh = next((index for index, word in enumerate(words) if word.endswith("/codex_mesh.py")), None)
    return mesh is not None and words[mesh + 1:mesh + 2] in (["start"], ["follow"])


def codex_seats(table: Mapping[int, Process]) -> list[Recipient]:
    """Each running Codex seat, by the name and session directory its turn was started with."""
    seats: dict[Recipient, None] = {}
    for process in table.values():
        words = process.arguments.split()
        options = dict(zip(words, words[1:]))
        name, directory = options.get("--name") or options.get("--to"), options.get("--session-dir")
        if is_codex_agent(process) and name and directory:
            seats[Recipient(name, "agent", directory)] = None
    return list(seats)


def unit_addresses(runner: showrunners.Showrunner) -> list[str]:
    """What reaches each unit of the showrunner: its session name now, or its unit id when no Claude runs.

    A unit id is no session's name, so the send reports that unit as having no live session.
    """
    try:
        return [unit.name or unit.unit for unit in units_of(runner)]
    except (OSError, ValueError) as error:
        print(f"broadcast: the units of {runner['slug']} could not be listed: {error}", file=sys.stderr)
        return []


def recipients(live: Collection[str]) -> list[Recipient]:
    """Every configured showrunner and unit director, then every other live session and running Codex seat."""
    registered = showrunners.registered_showrunners()
    # A showrunner that is not running has no name to look up: its production's slug stands for it,
    # which is no session's name, so the send reports it as having no live session.
    runners = [runner["session"] or runner["slug"] for runner in registered]
    units = dict.fromkeys(unit for runner in registered for unit in unit_addresses(runner) if unit not in runners)
    others = sorted(name for name in live if name not in runners and name not in units)
    return [*(Recipient(name, "showrunner") for name in runners), *(Recipient(name, "unit director") for name in units),
            *(Recipient(name, "agent") for name in others), *codex_seats(processes())]


def series(names: list[str], joiner: str) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {joiner} {names[-1]}"


def ending(told: Collection[Role]) -> str:
    """A message's last line: who was sent it, so nobody passes it on."""
    inside = [name for role, name in SAID.items() if role in told]
    outside = [name for role, name in SAID.items() if role not in told]
    return (f"Every {series(inside, 'and')} on this machine was sent this directly"
            + (f", and no {series(outside, 'or')}." if outside else "; do not pass it on."))


def send(session: str, sender: str, text: str, *options: str) -> str:
    """One message through send.py; the empty string when it was sent, else send.py's own line."""
    done = subprocess.run([sys.executable, str(SEND), "--to", session, "--from", sender, *options, "--text", text],
                          capture_output=True, text=True, check=False)
    return "" if done.returncode == 0 else (done.stdout or done.stderr).strip() or f"exit {done.returncode}"


def broadcast(sender: str, texts: Mapping[Role, str]) -> list[Delivery]:
    """Send each role its text, every live recipient at once."""
    live, last = live_sessions(), ending(texts)
    chosen = [one for one in recipients(live) if one.role in texts and one.session != sender]
    reached = [one for one in chosen if one.codex or one.session in live]

    def deliver(one: Recipient) -> str:
        return send(one.session, sender, f"{texts[one.role]}\n\n{last}",
                    *(("--codex", "--session-dir", one.codex) if one.codex else ()))

    with ThreadPoolExecutor(max_workers=AT_ONCE) as pool:
        problems = dict(zip(reached, pool.map(deliver, reached)))
    return [Delivery(one, "no live session", "") if one not in problems
            else Delivery(one, "NOT sent" if problems[one] else "sent", problems[one]) for one in chosen]


def report(deliveries: list[Delivery]) -> bool:
    """Print one line per recipient; true when every live one has the message."""
    for recipient, outcome, detail in deliveries:
        print(f"{recipient.session} - {recipient.role} - {outcome}" + (f": {detail}" if detail else ""))
    return all(delivery.outcome != "NOT sent" for delivery in deliveries)


def main(arguments: list[str]) -> int:
    flags = arguments[2::2]
    if len(arguments) < 4 or len(arguments) % 2 or arguments[0] != "--from" or not FLAGS.keys() >= set(flags):
        print(USAGE, file=sys.stderr)
        return 2
    sender, texts = arguments[1], dict[Role, str]()
    for flag, text in zip(flags, arguments[3::2]):
        texts.update(dict.fromkeys(FLAGS[flag], text))
    try:
        started = time.monotonic()
        deliveries = broadcast(sender, texts)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    every = report(deliveries)
    print(f"{sum(delivery.outcome == 'sent' for delivery in deliveries)} sent in {time.monotonic() - started:.0f} s")
    return 0 if every else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
