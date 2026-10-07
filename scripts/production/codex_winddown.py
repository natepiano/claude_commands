#!/usr/bin/env python3
"""Wind Codex agents down on every showrunner, and count the ones still running.

A running Codex agent is one live `codex_mesh.py start|follow` turn or one plain
`codex exec`. It belongs to the nearest ancestor that is a named Claude session.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import showrunners
from live_units import live_units

MESSAGE = Path(__file__).resolve().parent.parent / "message"
sys.path.insert(0, str(MESSAGE))
from sessions import SessionRecord, live_session, read_session  # noqa: E402

NOTIFIER = Path(os.environ.get("CODEX_WINDDOWN_NOTIFIER") or MESSAGE / "notifier.sh")
SEND = Path(os.environ.get("CODEX_WINDDOWN_SEND") or MESSAGE / "send.py")
PROMPTS = Path(os.environ.get("CODEX_WINDDOWN_STATE") or Path.home() / ".local/state/codex-winddown")
INSTANCE = "codex-count-"
EVERY_MINUTES = 2
NO_SESSION = "no session"
USAGE = "usage: codex_winddown.py status | count <showrunner session> | (start | clear) --from <your session>"

WIND_DOWN = (
    "Codex wind-down, from the user (/codex_winddown start in {sender}, {time}): let every running"
    " Codex agent finish and launch no new one until the user gives the all clear. Pass this to your"
    " unit directors. Every {minutes} minutes a \"Codex count\" message arrives; do what it says and"
    " nothing else in that turn. Protocol: ~/.claude/commands/codex_winddown.md."
)
TICK = (
    "Codex count (every {minutes} minutes until the user's all clear). Run `python3 {script} count"
    " {session}` and paste its output word for word as your reply. Do no other work in this turn."
)
ALL_CLEAR = (
    "All clear, from the user (/codex_winddown clear in {sender}, {time}): the Codex wind-down is"
    " over. Codex agents may launch again, and the Codex count has stopped. Pass this to your unit"
    " directors."
)


class Process(NamedTuple):
    pid: int
    parent: int
    command: str
    arguments: str


def processes() -> dict[int, Process]:
    listing = subprocess.run(["ps", "-eo", "pid=,ppid=,comm=,args="], capture_output=True, text=True,
                             check=True)
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


def live_sessions() -> list[SessionRecord]:
    records = (read_session(path) for path in showrunners.SESSIONS_DIR.glob("*.json"))
    return [record for record in records if record is not None and record["name"] and live_session(record)]


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


def listed(session: str, units: list[str], counts: Mapping[str, int]) -> dict[str, int]:
    """A showrunner's lines: every unit director, and itself when it runs agents or has no units."""
    rows = {unit: counts.get(unit, 0) for unit in units}
    if not rows or counts.get(session, 0):
        rows[session] = counts.get(session, 0)
    return rows


def render(heading: str, rows: Mapping[str, int]) -> str:
    lines = [f"{name} - {rows[name]}" for name in sorted(rows, key=str.casefold)]
    return "\n".join([heading, "", "```", *lines, "```"])


def current_counts() -> Counter[str]:
    return agent_counts(processes(), {record["pid"]: record["name"] for record in live_sessions()})


def clock(zone: str) -> str:
    return datetime.now(ZoneInfo(zone)).strftime("%H:%M %Z")


def count(session: str) -> int:
    runner = next((item for item in showrunners.load_settings()["showrunners"] if item["session"] == session),
                  None)
    if runner is None:
        print(f"showrunner absent from config: {session}", file=sys.stderr)
        return 1
    rows = listed(session, live_units(session), current_counts())
    print(render(f"Codex agents, {clock(runner['zone'])}", rows))
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
    print(render("Codex agents on this machine", rows))
    return 0


def instance_name(session: str) -> str:
    return INSTANCE + re.sub(r"[^A-Za-z0-9._-]", "-", session)


def send(session: str, sender: str, text: str) -> bool:
    done = subprocess.run([sys.executable, str(SEND), "--to", session, "--from", sender, "--text", text],
                          capture_output=True, text=True, check=False)
    return done.returncode == 0


def start_count(record: SessionRecord) -> bool:
    PROMPTS.mkdir(parents=True, exist_ok=True)
    prompt = PROMPTS / f"{instance_name(record['name'])}.txt"
    _ = prompt.write_text(TICK.format(minutes=EVERY_MINUTES, script=Path(__file__).resolve(),
                                      session=shlex.quote(record["name"])) + "\n", encoding="utf-8")
    done = subprocess.run(["zsh", str(NOTIFIER), "new", instance_name(record["name"]), "--every",
                           str(EVERY_MINUTES), "--to", f"session:{record['sessionId']}", "--prompt-file",
                           str(prompt), "--from", "codex-winddown"], capture_output=True, text=True, check=False)
    return done.returncode == 0


def start(sender: str) -> int:
    live = {record["name"]: record for record in live_sessions()}
    failed = False
    for runner in showrunners.load_settings()["showrunners"]:
        record = live.get(runner["session"])
        if record is None:
            print(f"{runner['session']}: no live session, skipped")
            continue
        text = WIND_DOWN.format(sender=sender, time=clock(runner["zone"]), minutes=EVERY_MINUTES)
        told, counting = send(runner["session"], sender, text), start_count(record)
        failed = failed or not (told and counting)
        counted = f"counting every {EVERY_MINUTES} minutes" if counting else "count NOT started"
        print(f"{runner['session']}: {'told' if told else 'NOT told'}, {counted}")
    return 1 if failed else 0


def clear(sender: str) -> int:
    failed = False
    for name in instances():
        done = subprocess.run(["zsh", str(NOTIFIER), "remove", name], capture_output=True, text=True, check=False)
        (PROMPTS / f"{name}.txt").unlink(missing_ok=True)
        failed = failed or done.returncode != 0
        print(f"{name.removeprefix(INSTANCE)}: {'count stopped' if done.returncode == 0 else 'count NOT stopped'}")
    live = {record["name"] for record in live_sessions()}
    for runner in showrunners.load_settings()["showrunners"]:
        if runner["session"] in live:
            told = send(runner["session"], sender, ALL_CLEAR.format(sender=sender, time=clock(runner["zone"])))
            failed = failed or not told
            print(f"{runner['session']}: {'told the all clear' if told else 'NOT told'}")
    return 1 if failed else 0


def main(arguments: list[str]) -> int:
    try:
        match arguments:
            case ["status"]:
                return status()
            case ["count", session]:
                return count(session)
            case ["start", "--from", sender]:
                return start(sender)
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
