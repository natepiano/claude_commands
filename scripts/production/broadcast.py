#!/usr/bin/env python3
"""Send roster groups a message, with one concurrent delivery per recipient.

  broadcast.py [--here] --from NAME (--all TEXT | --showrunners TEXT | --units TEXT |
                           --agents TEXT | [--of UNIT] --workers TEXT | --top-level TEXT)...

Overlapping groups receive the text of the last selecting flag. The sender is
left out by session or Codex thread identity. Every message names its groups so
recipients know it was sent directly.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, cast

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
import roster  # noqa: E402

SEND = Path(os.environ.get("BROADCAST_SEND") or MESSAGE / "send.py")
USAGE = ("usage: broadcast.py [--here] --from <your session> (--all TEXT | --showrunners TEXT | --units TEXT"
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
    """Every worker."""


@dataclass(frozen=True)
class WorkersOfUnit:
    address: str
    unit: str
    machine: str


@dataclass(frozen=True)
class TopLevel:
    """Claude sessions that receive top-level announcements."""


BroadcastGroup = AllRoles | Showrunners | UnitDirectors | Agents | Workers | WorkersOfUnit | TopLevel
Outcome = Literal["sent", "no live session", "NOT sent"]
FLAGS: dict[str, BroadcastGroup] = {
    "--all": AllRoles(), "--showrunners": Showrunners(), "--units": UnitDirectors(),
    "--agents": Agents(), "--workers": Workers(), "--top-level": TopLevel(),
    # Internal replay spelling selects all workers without inheriting an earlier --of.
    "--all-workers": Workers(),
}


class Recipient(NamedTuple):
    session: str
    address: str = ""


@dataclass(frozen=True)
class PeerOutput:
    host: str
    line: str


@dataclass(frozen=True)
class GroupWithoutMembers:
    group: BroadcastGroup


class Delivery(NamedTuple):
    recipient: Recipient | PeerOutput | GroupWithoutMembers
    outcome: Outcome
    detail: str


def recipients() -> list[roster.RosterEntry]:
    """Read the roster once, retaining readable rows when a source fails."""
    result = roster.roster(os.environ, roster.ThisMachine())
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


def ending(told: Collection[BroadcastGroup], scope: roster.RosterScope = roster.ALL_MACHINES) -> str:
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
    machines = os.environ.get("BROADCAST_CLOSING_MACHINES", "")
    if not machines:
        machines = (f"{roster.machine_name()} and {roster.remote.other_machine()}"
                    if isinstance(scope, roster.AllMachines) else "this machine")
    return f"Sent directly to {series(names, 'and')} on {machines}; do not pass it on."


def send(session: str, sender: str, text: str, *options: str) -> str:
    """One message through send.py; the empty string when it was sent, else send.py's own line."""
    done = subprocess.run([sys.executable, str(SEND), "--to", session, "--from", sender, *options, "--text", text],
                          capture_output=True, text=True, check=False)
    return "" if done.returncode == 0 else (done.stdout or done.stderr).strip() or f"exit {done.returncode}"


def _unreachable(entry: roster.RosterEntry, reach: roster.CannotReceiveMessages) -> str:
    if entry.reach != reach:
        raise ValueError("refusal reach must belong to its row")
    return f"send.py: {roster.cannot_receive_message_text(entry)}"


def _broadcast(
    sender: str, texts: Mapping[BroadcastGroup, str], entries: Sequence[roster.RosterEntry],
    identity: roster.SenderIdentity,
    scope: roster.RosterScope = roster.ALL_MACHINES,
) -> list[Delivery]:
    if not texts:
        return []
    last = ending(texts, scope)
    chosen: list[tuple[roster.RosterEntry, str]] = []
    for entry in entries:
        if roster.is_sender(entry, identity):
            continue
        versions = [text for group, text in texts.items() if selects(group, entry)]
        if versions:
            chosen.append((entry, versions[-1]))

    def deliver(chosen_entry: tuple[roster.RosterEntry, str]) -> Delivery:
        entry, text = chosen_entry
        shown = roster.shown_name(entry)
        recipient = Recipient(shown.render(), shown.address)
        match entry.reach:
            case roster.CannotReceiveMessages():
                return Delivery(recipient, "no live session", _unreachable(entry, entry.reach))
            case roster.Messageable(address):
                try:
                    problem = send(address, sender, f"{text}\n\n{last}", "--here")
                except OSError as error:
                    problem = str(error)
                return Delivery(recipient, "NOT sent" if problem else "sent", problem)

    with ThreadPoolExecutor(max_workers=AT_ONCE) as pool:
        return list(pool.map(deliver, chosen))


def broadcast(
    sender: str, texts: Mapping[BroadcastGroup, str], *, scope: roster.RosterScope = roster.ALL_MACHINES,
) -> list[Delivery]:
    """The same local and peer orchestration used by the command line."""
    identity = roster.sender_identity(os.environ)
    return _orchestrate(sender, texts, recipients(), identity, scope)


def report(deliveries: list[Delivery]) -> bool:
    """Print one line per recipient; true when every live one has the message."""
    for recipient, outcome, detail in deliveries:
        match recipient:
            case PeerOutput(host, line):
                print(f"{host}: {line}")
            case GroupWithoutMembers(group):
                print(f"{_group_name(group)} - none on this machine")
            case Recipient(session, address):
                label = f"{session} [{address}]" if address and address != session else session
                print(f"{label} - {outcome}" + (f": {detail}" if detail else ""))
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
                        + f" ({entry.name}, {entry.machine}): {address}"
                        for entry, address in known]
        reason = f"{target} matches more than one unit director:" if matches else f"unknown unit {target}; known units:"
        raise ValueError("\n".join([reason, *descriptions]))
    director, address = matches[0]
    return WorkersOfUnit(address,
                         director.director.unit if isinstance(director.director, roster.DirectsUnit) else director.name,
                         director.machine)


def _group_name(group: BroadcastGroup) -> str:
    match group:
        case AllRoles():
            return "all roles"
        case Showrunners():
            return "showrunners"
        case UnitDirectors():
            return "unit directors"
        case Agents():
            return "agents"
        case Workers():
            return "workers"
        case WorkersOfUnit(unit=unit):
            return f"workers of {unit}"
        case TopLevel():
            return "top level"


def _machine_texts(
    texts: Mapping[BroadcastGroup, str], *, here: bool,
) -> dict[BroadcastGroup, str]:
    return {group: text for group, text in texts.items()
            if not isinstance(group, WorkersOfUnit)
            or (group.machine == roster.machine_name()) == here}


def _remote_arguments(sender: str, texts: Mapping[BroadcastGroup, str]) -> list[str]:
    arguments = ["--here", "--from", sender]
    for group, text in texts.items():
        if isinstance(group, WorkersOfUnit):
            arguments.extend(("--of", group.address, "--workers", text))
        elif isinstance(group, Workers):
            arguments.extend(("--all-workers", text))
        else:
            flag = next(flag for flag, candidate in FLAGS.items() if candidate == group)
            arguments.extend((flag, text))
    return arguments


def _remote_broadcast(sender: str, texts: Mapping[BroadcastGroup, str]) -> list[Delivery]:
    host = roster.remote.other_machine()
    environment = ["env", "-u", "CODEX_THREAD_ID", "-u", "CLAUDE_CODE_SESSION_ID"]
    if "BROADCAST_SEND" in os.environ:
        environment.append(f"BROADCAST_SEND={os.environ['BROADCAST_SEND']}")
    environment.append(
        f"BROADCAST_CLOSING_MACHINES={os.environ.get('BROADCAST_CLOSING_MACHINES') or roster.machine_name() + ' and ' + host}"
    )
    command = (shlex.join(environment)
               + ' "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/broadcast.py" '
               + shlex.join(_remote_arguments(sender, texts)))
    try:
        done = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                              capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        why = "timed out" if isinstance(error, subprocess.TimeoutExpired) else str(error).splitlines()[0]
        return [Delivery(PeerOutput(host, f"NOT sent: {why}"), "NOT sent", "")]
    if done.returncode == 255:
        why = (done.stderr or done.stdout or "not reached").strip().splitlines()[0]
        return [Delivery(PeerOutput(host, f"NOT sent: {why}"), "NOT sent", "")]
    deliveries: list[Delivery] = []
    for line in done.stdout.splitlines():
        parts = line.split(" - ", 2)
        status = parts[2].partition(":")[0] if len(parts) == 3 else ""
        outcome = cast(Outcome, status) if status in {"sent", "NOT sent", "no live session"} else "no live session"
        deliveries.append(Delivery(PeerOutput(host, line), outcome, ""))
    for line in done.stderr.splitlines():
        deliveries.append(Delivery(PeerOutput(host, line), "no live session", ""))
    if done.returncode != 0 and not any(delivery.outcome == "NOT sent" for delivery in deliveries):
        deliveries.append(Delivery(PeerOutput(host, f"NOT sent: exit {done.returncode}"), "NOT sent", ""))
    return deliveries


def _orchestrate(
    sender: str, texts: Mapping[BroadcastGroup, str], entries: Sequence[roster.RosterEntry],
    identity: roster.SenderIdentity, scope: roster.RosterScope,
) -> list[Delivery]:
    if not texts:
        return []
    here_texts = _machine_texts(texts, here=True)
    peer_texts = _machine_texts(texts, here=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        peer = (pool.submit(_remote_broadcast, sender, peer_texts)
                if isinstance(scope, roster.AllMachines) and peer_texts else None)
        deliveries = _broadcast(sender, here_texts, entries, identity, scope)
        for group in here_texts:
            if not any(selects(group, entry) and not roster.is_sender(entry, identity) for entry in entries):
                deliveries.append(Delivery(GroupWithoutMembers(group), "no live session", ""))
        if peer is not None:
            deliveries.extend(peer.result())
    return deliveries


def main(arguments: list[str]) -> int:
    started = time.monotonic()
    pairs: list[tuple[str, str]] = []
    sender = ""
    scope: roster.RosterScope = roster.AllMachines()
    index = 0
    while index < len(arguments):
        flag = arguments[index]
        if flag == "--here":
            scope = roster.ThisMachine()
            index += 1
            continue
        if index + 1 >= len(arguments) or flag not in FLAGS.keys() | {"--of", "--from"}:
            print(USAGE, file=sys.stderr)
            return 2
        value = arguments[index + 1]
        if flag == "--from":
            sender = value
        else:
            pairs.append((flag, value))
        index += 2
    if not sender or not any(flag in FLAGS for flag, _ in pairs):
        print(USAGE, file=sys.stderr)
        return 2
    identity = roster.sender_identity(os.environ)
    if any(flag == "--of" for flag, _ in pairs):
        result = roster.roster(os.environ, scope)
        for problem in result.problems:
            print(f"broadcast: roster: {problem}", file=sys.stderr)
        host = roster.remote.other_machine()
        failures = [problem for problem in result.problems if problem.startswith(f"{host}: not reached:")]
        if failures:
            # A partial roster cannot prove that a director name is unique.
            _ = report([Delivery(PeerOutput(host, f"NOT sent: {failures[0]}"), "NOT sent", "")])
            return 1
        unit_entries = result.entries
        entries = [entry for entry in unit_entries if entry.machine == roster.machine_name()]
    else:
        entries = recipients()
        unit_entries = entries
    texts: dict[BroadcastGroup, str] = {}
    workers: Workers | WorkersOfUnit = Workers()
    scope_pending = False
    try:
        for flag, text in pairs:
            if flag == "--of":
                workers = _unit_group(text, unit_entries)
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
    deliveries = _orchestrate(sender, texts, entries, identity, scope)
    every = report(deliveries)
    print(f"{sum(delivery.outcome == 'sent' for delivery in deliveries)} sent in {time.monotonic() - started:.0f} s")
    print(ending(texts, scope))
    return 0 if every else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
