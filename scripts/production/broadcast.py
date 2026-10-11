#!/usr/bin/env python3
"""Send roster groups a message, with one concurrent delivery per recipient.

  broadcast.py --from NAME (--all TEXT | --showrunners TEXT | --units TEXT |
                           --agents TEXT | [--of UNIT] --workers TEXT | --top-level TEXT)...

Overlapping groups receive the text of the last selecting flag. The sender is
left out by session or Codex thread identity. Every message names its groups so
recipients know it was sent directly.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
import roster  # noqa: E402

SEND = Path(os.environ.get("BROADCAST_SEND") or MESSAGE / "send.py")
USAGE = ("usage: broadcast.py --from <your session> (--all TEXT | --showrunners TEXT | --units TEXT"
         + " | --agents TEXT | [--of UNIT] --workers TEXT | --top-level TEXT)...")
# One relay peaked at 272 MB when measured (2026-10-07), so this many together hold about 6.5 GB.
AT_ONCE = 24


@dataclass(frozen=True)
class AllRoles:
    """Every role in the roster."""


@dataclass(frozen=True)
class Showrunners:
    """Every showrunner."""


@dataclass(frozen=True)
class UnitDirectors:
    """Every unit director."""


@dataclass(frozen=True)
class Agents:
    """Every worker and freestanding session, Claude or Codex."""


@dataclass(frozen=True)
class Workers:
    """Every delegate worker."""


@dataclass(frozen=True)
class WorkersOfUnit:
    address: str
    unit: str


@dataclass(frozen=True)
class TopLevel:
    """Claude sessions that receive top-level announcements."""


BroadcastGroup = AllRoles | Showrunners | UnitDirectors | Agents | Workers | WorkersOfUnit | TopLevel
Outcome = Literal["sent", "no live session", "NOT sent"]
FLAGS: dict[str, BroadcastGroup] = {
    "--all": AllRoles(), "--showrunners": Showrunners(), "--units": UnitDirectors(),
    "--agents": Agents(), "--workers": Workers(), "--top-level": TopLevel(),
}


class Recipient(NamedTuple):
    session: str
    role: str


class Delivery(NamedTuple):
    recipient: Recipient
    outcome: Outcome
    detail: str


def recipients() -> list[roster.RosterEntry]:
    """Read the roster once, retaining readable rows when a source fails."""
    result = roster.roster(os.environ)
    for problem in result.problems:
        print(f"broadcast: roster: {problem}", file=sys.stderr)
    return result.entries


def selects(group: BroadcastGroup, entry: roster.RosterEntry) -> bool:
    match group:
        case AllRoles():
            return True
        case Showrunners():
            return entry.role == "showrunner"
        case UnitDirectors():
            return entry.role == "unit director"
        case Agents():
            return entry.role in ("worker", "freestanding")
        case Workers():
            return entry.role == "worker"
        case WorkersOfUnit(address):
            return (entry.role == "worker" and isinstance(entry.director, roster.UnderDirector)
                    and entry.director.address == address)
        case TopLevel():
            return roster.is_top_level(entry)


def series(names: list[str], joiner: str) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} {joiner} {names[-1]}"


def ending(told: Collection[BroadcastGroup]) -> str:
    """A message's last line names the selected groups, which may overlap."""
    names: list[str] = []
    for group in told:
        match group:
            case AllRoles():
                labels = ["showrunners", "unit directors", "agents"]
            case Showrunners():
                labels = ["showrunners"]
            case UnitDirectors():
                labels = ["unit directors"]
            case Agents():
                labels = ["agents"]
            case Workers():
                labels = ["workers"]
            case WorkersOfUnit(unit=unit):
                labels = [f"workers of {unit}"]
            case TopLevel():
                labels = ["top level"]
        names.extend(label for label in labels if label not in names)
    return f"Sent directly to {series(names, 'and')} on this machine; do not pass it on."


def send(session: str, sender: str, text: str, *options: str) -> str:
    """One message through send.py; the empty string when it was sent, else send.py's own line."""
    done = subprocess.run([sys.executable, str(SEND), "--to", session, "--from", sender, *options, "--text", text],
                          capture_output=True, text=True, check=False)
    return "" if done.returncode == 0 else (done.stdout or done.stderr).strip() or f"exit {done.returncode}"


def _unreachable(entry: roster.RosterEntry, reach: roster.CannotReceiveMessages) -> str:
    match entry.director:
        case roster.UnderDirector(name=director):
            next_step = f"contact its unit director {director} or wait"
        case _:
            next_step = "no unit director is running for it"
    return f"send.py: {entry.name} cannot take messages ({reach.reason}); {next_step}"


def _broadcast(
    sender: str, texts: Mapping[BroadcastGroup, str], entries: Sequence[roster.RosterEntry],
    identity: roster.SenderIdentity,
) -> list[Delivery]:
    if not texts:
        return []
    last = ending(texts)
    chosen: list[tuple[roster.RosterEntry, str]] = []
    for entry in entries:
        if roster.is_sender(entry, identity):
            continue
        versions = [text for group, text in texts.items() if selects(group, entry)]
        if versions:
            chosen.append((entry, versions[-1]))

    def deliver(chosen_entry: tuple[roster.RosterEntry, str]) -> Delivery:
        entry, text = chosen_entry
        recipient = Recipient(entry.name, entry.role)
        match entry.reach:
            case roster.CannotReceiveMessages():
                return Delivery(recipient, "no live session", _unreachable(entry, entry.reach))
            case roster.Messageable(address):
                problem = send(address, sender, f"{text}\n\n{last}")
                return Delivery(recipient, "NOT sent" if problem else "sent", problem)

    with ThreadPoolExecutor(max_workers=AT_ONCE) as pool:
        return list(pool.map(deliver, chosen))


def broadcast(sender: str, texts: Mapping[BroadcastGroup, str]) -> list[Delivery]:
    """Send the last matching group's text to each roster member once."""
    identity = roster.sender_identity(os.environ)
    return _broadcast(sender, texts, recipients(), identity)


def report(deliveries: list[Delivery]) -> bool:
    """Print one line per recipient; true when every live one has the message."""
    for recipient, outcome, detail in deliveries:
        print(f"{recipient.session} - {recipient.role} - {outcome}" + (f": {detail}" if detail else ""))
    return all(delivery.outcome != "NOT sent" for delivery in deliveries)


def _unit_group(target: str, entries: Sequence[roster.RosterEntry]) -> WorkersOfUnit:
    directors = [(entry, f"session:{entry.session.session_id}") for entry in entries
                 if entry.role == "unit director" and isinstance(entry.session, roster.ClaudeSession)]
    matches = [(entry, address) for entry, address in directors
               if entry.name == target or address == target
               or (isinstance(entry.director, roster.DirectsUnit) and entry.director.unit == target)]
    if len(matches) != 1:
        known = matches if matches else directors
        descriptions = [f"  {entry.director.unit if isinstance(entry.director, roster.DirectsUnit) else entry.name}"
                        + f" ({entry.name}): {address}"
                        for entry, address in known]
        reason = f"{target} matches more than one unit director:" if matches else f"unknown unit {target}; known units:"
        raise ValueError("\n".join([reason, *descriptions]))
    director, address = matches[0]
    return WorkersOfUnit(address,
                         director.director.unit if isinstance(director.director, roster.DirectsUnit) else director.name)


def main(arguments: list[str]) -> int:
    flags = arguments[2::2]
    if (len(arguments) < 4 or len(arguments) % 2 or arguments[0] != "--from"
            or not (FLAGS.keys() | {"--of"}) >= set(flags)
            or not any(flag in FLAGS for flag in flags)):
        print(USAGE, file=sys.stderr)
        return 2
    sender = arguments[1]
    texts: dict[BroadcastGroup, str] = {}
    started = time.monotonic()
    identity = roster.sender_identity(os.environ)
    entries = recipients()
    workers: Workers | WorkersOfUnit = Workers()
    scope_pending = False
    try:
        for flag, text in zip(flags, arguments[3::2]):
            if flag == "--of":
                workers = _unit_group(text, entries)
                scope_pending = True
                continue
            group = workers if flag == "--workers" else FLAGS[flag]
            if flag == "--workers":
                scope_pending = False
            _ = texts.pop(group, None)
            texts[group] = text
        if scope_pending:
            print(USAGE, file=sys.stderr)
            return 2
    except ValueError as error:
        print(f"broadcast: {error}", file=sys.stderr)
        return 2
    try:
        deliveries = _broadcast(sender, texts, entries, identity)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    every = report(deliveries)
    for group in texts:
        if any(selects(group, entry) and not roster.is_sender(entry, identity) for entry in entries):
            continue
        match group:
            case AllRoles():
                name = "all roles"
            case Showrunners():
                name = "showrunners"
            case UnitDirectors():
                name = "unit directors"
            case Agents():
                name = "agents"
            case Workers():
                name = "workers"
            case WorkersOfUnit(unit=unit):
                name = f"workers of {unit}"
            case TopLevel():
                name = "top level"
        print(f"{name} - none on this machine")
    print(f"{sum(delivery.outcome == 'sent' for delivery in deliveries)} sent in {time.monotonic() - started:.0f} s")
    return 0 if every else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
