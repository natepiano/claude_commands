#!/usr/bin/env python3
"""Wind Codex agents down on every showrunner, and count the ones still running.

A running Codex agent is one live `codex_mesh.py start|follow` turn or one plain
`codex exec`. It belongs to the nearest ancestor that is a named Claude session.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, NamedTuple, cast
from zoneinfo import ZoneInfo

import showrunners
from live_units import live_units

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
from sessions import SessionRecord, live_session, read_session  # noqa: E402

NOTIFIER = Path(os.environ.get("CODEX_WINDDOWN_NOTIFIER") or MESSAGE / "notifier.sh")
SEND = Path(os.environ.get("CODEX_WINDDOWN_SEND") or MESSAGE / "send.py")
PUSH = Path(os.environ.get("CODEX_WINDDOWN_PUSH") or MESSAGE.parent / "notify/pushover.py")
PS = shlex.split(os.environ.get("CODEX_WINDDOWN_PS") or "ps -eo pid=,ppid=,comm=,args=")
STATE = Path(os.environ.get("CODEX_WINDDOWN_STATE") or Path.home() / ".local/state/codex-winddown")
PROJECTIONS = "projections.json"
QUIET = "quiet"
INSTANCE = "codex-count-"
EVERY_MINUTES = 2
NO_SESSION = "no session"
UNMEASURED: Final = "unmeasured"
USAGE = ("usage: codex_winddown.py status | count <showrunner session> | eta <unit session> <minutes|unmeasured>"
         + " | (start | triage | clear) --from <your session>")

# A unit's answer to "when does your last Codex agent finish": None while the
# answer is owed, UNMEASURED, or the finish time as an epoch.
Projection = float | Literal["unmeasured"] | None

WIND_DOWN = (
    "Codex wind-down, from the user (/codex_winddown start in {sender}, {time}): let every running"
    " Codex agent finish and launch no new one until the user gives the all clear. Unit directors are"
    " encouraged to continue work on their own. Pass this to your unit directors. Every {minutes} minutes a \"Codex count\" message arrives; do what it says and"
    " nothing else in that turn. Protocol: ~/.claude/commands/codex_winddown.md."
)
TICK = (
    "Codex count (every {minutes} minutes until the user's all clear). Run `python3 {script} count"
    " {session}` and paste its output word for word as your reply. Do no other work in this turn."
)
ASK = (
    "Codex wind-down, from {showrunner}: Codex agents running under you: {agents}. Estimate when the"
    " last one finishes and run `python3 {script} eta {unit} <minutes from now>`, or `python3 {script}"
    " eta {unit} unmeasured` when you cannot tell. Run it again when the estimate changes. Send no"
    " other reply."
)
AT_ZERO = (
    "Codex wind-down, from {showrunner}: your Codex agents have all finished. From the user: you are"
    " encouraged to do work on your own, or to launch Claude agents for short time frames, until the"
    " all clear."
)
TRIAGE = (
    "Codex wind-down, from the user (/codex_winddown triage in {sender}, {time}): \"you need to tell"
    " all of the other seats to figure out which ones can be stopped now and added to a resume list vs"
    " which really should finish right now\". Pass this to every unit director with a running Codex"
    " agent: stop each one that can stop, keep what it wrote, and put it on a resume list (the seat,"
    " its work order, what is left); let the rest finish."
)
ALL_CLEAR = (
    "All clear, from the user (/codex_winddown clear in {sender}, {time}): the Codex wind-down is"
    " over. Codex agents may launch again, starting with what the resume lists hold, and the Codex"
    " count has stopped. Pass this to your unit directors."
)


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


def live_sessions() -> dict[str, SessionRecord]:
    """Each live named session, the newest record winning a shared name."""
    newest: dict[str, SessionRecord] = {}
    for path in showrunners.SESSIONS_DIR.glob("*.json"):
        record = read_session(path)
        if record is None or not record["name"] or not live_session(record):
            continue
        if record["name"] not in newest or record["updatedAt"] > newest[record["name"]]["updatedAt"]:
            newest[record["name"]] = record
    return newest


def owner(process: Process, table: Mapping[int, Process], names: Mapping[int, str]) -> str:
    seen: set[int] = set()
    current: Process | None = process
    while current is not None and current.pid not in seen:
        if current.pid in names:
            return names[current.pid]
        seen.add(current.pid)
        current = table.get(current.parent)
    return NO_SESSION


def agent_counts(table: Mapping[int, Process], names: Mapping[int, str]) -> Counter[str]:
    return Counter(owner(process, table, names) for process in table.values() if is_codex_agent(process))


def current_counts() -> Counter[str]:
    return agent_counts(processes(), {record["pid"]: name for name, record in live_sessions().items()})


def listed(session: str, units: list[str], counts: Mapping[str, int]) -> dict[str, int]:
    """A showrunner's lines: every unit director, and itself when it runs agents or has no units."""
    rows = {unit: counts.get(unit, 0) for unit in units}
    if not rows or counts.get(session, 0):
        rows[session] = counts.get(session, 0)
    return rows


def read_projections() -> dict[str, Projection]:
    try:
        raw = cast(object, json.loads((STATE / PROJECTIONS).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    kept: dict[str, Projection] = {}
    for unit, answer in (cast(dict[str, object], raw) if isinstance(raw, dict) else {}).items():
        if answer is None:
            kept[unit] = None
        elif answer == UNMEASURED:
            kept[unit] = UNMEASURED
        elif isinstance(answer, (int, float)) and not isinstance(answer, bool):
            kept[unit] = float(answer)
    return kept


@contextmanager
def projections() -> Generator[dict[str, Projection]]:
    """The projections under their lock; the caller's changes are saved on exit."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        held = read_projections()
        yield held
        temporary = STATE / f"{PROJECTIONS}.tmp"
        _ = temporary.write_text(json.dumps(held), encoding="utf-8")
        _ = temporary.replace(STATE / PROJECTIONS)


def projected(unit: str, held: Mapping[str, Projection], now: float) -> str:
    if unit not in held:
        return "ETA Unmeasured"
    answer = held[unit]
    if answer is None:
        return "asked, answer owed"
    if isinstance(answer, str) or answer <= now:
        return "ETA Unmeasured"
    minutes = math.ceil((answer - now) / 60)
    return f"likely finishes in {minutes} minute{'' if minutes == 1 else 's'}"


def render(heading: str, rows: Mapping[str, int], held: Mapping[str, Projection], now: float) -> str:
    lines = [f"{name} - {rows[name]}" + (f" - {projected(name, held, now)}" if rows[name] else "")
             for name in sorted(rows, key=str.casefold)]
    return "\n".join([heading, "", "```", *lines, "```"])


def clock(zone: str) -> str:
    return datetime.now(ZoneInfo(zone)).strftime("%H:%M %Z")


def send(session: str, sender: str, text: str) -> bool:
    done = subprocess.run([sys.executable, str(SEND), "--to", session, "--from", sender, "--text", text],
                          capture_output=True, text=True, check=False)
    return done.returncode == 0


def needs_asking(unit: str, held: Mapping[str, Projection], now: float) -> bool:
    answer = held.get(unit, 0.0)
    return isinstance(answer, float) and answer <= now


def ask(showrunner: str, unit: str, agents: int) -> bool:
    return send(unit, showrunner, ASK.format(showrunner=showrunner, agents=agents, script=Path(__file__).resolve(),
                                             unit=shlex.quote(unit)))


def refresh(showrunner: str, units: list[str], rows: Mapping[str, int], now: float) -> dict[str, Projection]:
    """Ask each unit whose running agents have no current projection, and tell each one that reached none."""
    held = read_projections()
    asked = [unit for unit in units
             if rows[unit] and needs_asking(unit, held, now) and ask(showrunner, unit, rows[unit])]
    with projections() as saved:
        finished = [unit for unit in units if not rows[unit] and unit in saved]
        for unit in finished:
            del saved[unit]
        saved.update(dict.fromkeys(asked))
        current = dict(saved)
    for unit in finished:
        _ = send(unit, showrunner, AT_ZERO.format(showrunner=showrunner))
    return current


def announce_quiet(running: int) -> None:
    """Push the user once when a wind-down has no Codex agent left on the machine; a later agent re-arms it."""
    if running or not instances():
        (STATE / QUIET).unlink(missing_ok=True)
        return
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / QUIET).touch(exist_ok=False)
    except FileExistsError:
        return
    text = f"No Codex agent is running on {socket.gethostname()}. Reset when ready, then /codex_winddown clear."
    _ = subprocess.run([sys.executable, str(PUSH), "--priority", "1", "Codex wind-down", text],
                       capture_output=True, text=True, check=False)


def count(session: str) -> int:
    runner = next((item for item in showrunners.load_settings()["showrunners"] if item["session"] == session),
                  None)
    if runner is None:
        print(f"showrunner absent from config: {session}", file=sys.stderr)
        return 1
    units, counts = live_units(session), current_counts()
    rows = listed(session, units, counts)
    held = refresh(session, units, rows, time.time())
    announce_quiet(counts.total())
    print(render(f"Codex agents, {clock(runner['zone'])}", rows, held, time.time()))
    return 0


def record_eta(unit: str, answer: str) -> int:
    if answer != UNMEASURED and not (answer.isascii() and answer.isdigit()):
        print(USAGE, file=sys.stderr)
        return 2
    with projections() as held:
        held[unit] = UNMEASURED if answer == UNMEASURED else time.time() + 60 * int(answer)
    print(f"{unit}: recorded")
    return 0


def instances() -> list[str]:
    return sorted(path.name for path in showrunners.NOTIFIER_STATE_DIR.glob(f"{INSTANCE}*") if path.is_dir())


def status() -> int:
    counts = current_counts()
    rows: dict[str, int] = {name: number for name, number in counts.items() if number}
    for runner in showrunners.load_settings()["showrunners"]:
        rows.update(listed(runner["session"], live_units(runner["session"]), counts))
    counting = ", ".join(name.removeprefix(INSTANCE) for name in instances())
    print(f"Wind-down: {'on for ' + counting if counting else 'off'}")
    print(render("Codex agents on this machine", rows, read_projections(), time.time()))
    return 0


def instance_name(session: str) -> str:
    return INSTANCE + re.sub(r"[^A-Za-z0-9._-]", "-", session)


def tell_showrunners(sender: str, message: str, told: str) -> bool:
    """Send every live showrunner the message and report each; true when all of them have it."""
    live, every = live_sessions(), True
    for runner in showrunners.load_settings()["showrunners"]:
        if runner["session"] not in live:
            print(f"{runner['session']}: no live session, skipped")
            continue
        sent = send(runner["session"], sender,
                    message.format(sender=sender, time=clock(runner["zone"]), minutes=EVERY_MINUTES))
        every = every and sent
        print(f"{runner['session']}: {told if sent else 'NOT told'}")
    return every


def start_count(record: SessionRecord) -> bool:
    prompt = STATE / f"{instance_name(record['name'])}.txt"
    _ = prompt.write_text(TICK.format(minutes=EVERY_MINUTES, script=Path(__file__).resolve(),
                                      session=shlex.quote(record["name"])) + "\n", encoding="utf-8")
    done = subprocess.run(["zsh", str(NOTIFIER), "new", instance_name(record["name"]), "--every",
                           str(EVERY_MINUTES), "--to", f"session:{record['sessionId']}", "--prompt-file",
                           str(prompt), "--from", "codex-winddown"], capture_output=True, text=True, check=False)
    return done.returncode == 0


def start(sender: str) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    for name in (PROJECTIONS, QUIET):
        (STATE / name).unlink(missing_ok=True)
    every = tell_showrunners(sender, WIND_DOWN, "told to wind down")
    live = live_sessions()
    for runner in showrunners.load_settings()["showrunners"]:
        if runner["session"] in live:
            counting = start_count(live[runner["session"]])
            every = every and counting
            print(f"{runner['session']}: "
                  + (f"counting every {EVERY_MINUTES} minutes" if counting else "count NOT started"))
    return 0 if every else 1


def triage(sender: str) -> int:
    if not instances():
        print("No wind-down is on. Run /codex_winddown start first.", file=sys.stderr)
        return 1
    return 0 if tell_showrunners(sender, TRIAGE, "told to sort its agents") else 1


def clear(sender: str) -> int:
    every = True
    for name in instances():
        done = subprocess.run(["zsh", str(NOTIFIER), "remove", name], capture_output=True, text=True, check=False)
        (STATE / f"{name}.txt").unlink(missing_ok=True)
        every = every and done.returncode == 0
        print(f"{name.removeprefix(INSTANCE)}: {'count stopped' if done.returncode == 0 else 'count NOT stopped'}")
    for name in (PROJECTIONS, QUIET):
        (STATE / name).unlink(missing_ok=True)
    return 0 if tell_showrunners(sender, ALL_CLEAR, "told the all clear") and every else 1


def main(arguments: list[str]) -> int:
    try:
        match arguments:
            case ["status"]:
                return status()
            case ["count", session]:
                return count(session)
            case ["eta", unit, answer]:
                return record_eta(unit, answer)
            case ["start", "--from", sender]:
                return start(sender)
            case ["triage", "--from", sender]:
                return triage(sender)
            case ["clear", "--from", sender]:
                return clear(sender)
            case _:
                print(USAGE, file=sys.stderr)
                return 2
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
