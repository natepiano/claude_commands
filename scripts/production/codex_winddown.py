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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import broadcast
import process_table
from scripts.production import showrunners
from live_units import live_unit_names
from sessions import SessionRecord, live_sessions

NOTIFIER = Path(os.environ.get("CODEX_WINDDOWN_NOTIFIER") or broadcast.MESSAGE / "notifier.sh")
STATE = Path(os.environ.get("CODEX_WINDDOWN_STATE") or Path.home() / ".local/state/codex-winddown")
PROJECTIONS = "projections.json"
QUIET = "quiet"
INSTANCE = "codex-count-"
EVERY_MINUTES = 2
NO_SESSION = "no session"
UNMEASURED: Final = "unmeasured"
USAGE = ("usage: codex_winddown.py status | count <showrunner session> | eta <unit session> <minutes|unmeasured>"
         + " | (start | triage | clear) --from <your session>")


@dataclass(frozen=True)
class CompletionExpectedAt:
    epoch_seconds: float


@dataclass(frozen=True)
class CompletionUnmeasured:
    """The unit cannot estimate when its last Codex agent will finish."""


@dataclass(frozen=True)
class ProjectionAnswerOwed:
    """The unit has been asked for a completion projection but has not answered."""


@dataclass(frozen=True)
class ProjectionNotRequested:
    """No projection has been requested for the next group of agents."""


@dataclass(frozen=True)
class QuietCountObservation:
    """An agent count and the quiet generation in which it was observed."""

    running: int
    generation: int


@dataclass(frozen=True)
class QuietNoticeState:
    """The current quiet generation and whether its notice was sent."""

    generation: int
    sent: bool


ActiveProjection = CompletionExpectedAt | CompletionUnmeasured | ProjectionAnswerOwed


@dataclass(frozen=True)
class ZeroNoticeOwed:
    """A finished group's notice is owed, independent of the next group's projection."""

    resumed_projection: ActiveProjection | ProjectionNotRequested = ProjectionNotRequested()


Projection = ActiveProjection | ZeroNoticeOwed
_StoredActiveProjection = float | Literal["unmeasured"] | None
_StoredProjection = (
    _StoredActiveProjection | Literal["zero-notice-owed"]
    | dict[Literal["zero-notice-owed"], _StoredActiveProjection]
)

WIND_DOWN = (
    "Codex wind-down, from the user (/codex_winddown start in {sender}, {time}): let every running"
    " Codex agent finish and launch no new one until the user gives the all clear. Unit directors are"
    " encouraged to continue work on their own. Every {minutes} minutes a \"Codex count\" message"
    " arrives; do what it says and nothing else in that turn. Protocol:"
    " ~/.claude/commands/codex_winddown.md."
)
UNIT_WIND_DOWN = (
    "Codex wind-down, from the user (/codex_winddown start in {sender}, {time}): let every running"
    " Codex agent finish and launch no new one until the user gives the all clear. You are encouraged"
    " to continue work on your own, or to launch Claude agents for short time frames."
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
    " all of the other workers to figure out which ones can be stopped now and added to a resume list vs"
    " which really should finish right now\". Sort the Codex agents running under you: stop each one"
    " that can stop, keep what it wrote, and put it on a resume list (the worker, its work order, what is"
    " left); let the rest finish. With none running, there is nothing to do."
)
ALL_CLEAR = (
    "All clear, from the user (/codex_winddown clear in {sender}, {time}): we are back with Codex. The"
    " wind-down is over and the Codex count has stopped."
)
UNIT_CLEAR = (
    "All clear, from the user (/codex_winddown clear in {sender}, {time}): we are back with Codex. Start"
    " using Codex again, starting with what your resume list holds. It is okay to let a running Claude"
    " agent finish up."
)


def agent_counts(table: Mapping[int, process_table.Process], names: Mapping[int, str]) -> Counter[str]:
    return Counter(
        NO_SESSION if (name := process_table.nearest_named(process, table, names)) is None else name
        for process in table.values() if process_table.is_codex_agent(process)
    )


def current_counts() -> Counter[str]:
    names = {record["pid"]: record["name"] for record in live_sessions() if record["name"]}
    return agent_counts(process_table.snapshot(), names)


def listed(session: str, units: list[str], counts: Mapping[str, int]) -> dict[str, int]:
    """A showrunner's lines: every unit director, and itself when it runs agents or has no units."""
    rows = {unit: counts.get(unit, 0) for unit in units}
    if not rows or counts.get(session, 0):
        rows[session] = counts.get(session, 0)
    return rows


def _read_active_projection(answer: object) -> ActiveProjection | ProjectionNotRequested:
    if answer is None:
        return ProjectionAnswerOwed()
    if answer == UNMEASURED:
        return CompletionUnmeasured()
    if isinstance(answer, (int, float)) and not isinstance(answer, bool):
        return CompletionExpectedAt(float(answer))
    return ProjectionNotRequested()


def read_projections() -> dict[str, Projection]:
    try:
        raw = cast(object, json.loads((STATE / PROJECTIONS).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    kept: dict[str, Projection] = {}
    for unit, answer in (cast(dict[str, object], raw) if isinstance(raw, dict) else {}).items():
        if answer == "zero-notice-owed":
            kept[unit] = ZeroNoticeOwed()
        elif isinstance(answer, dict) and "zero-notice-owed" in answer:
            resumed = _read_active_projection(cast(dict[str, object], answer)["zero-notice-owed"])
            kept[unit] = ZeroNoticeOwed(resumed)
        else:
            active = _read_active_projection(cast(object, answer))
            if not isinstance(active, ProjectionNotRequested):
                kept[unit] = active
    return kept


def _stored_active_projection(projection: ActiveProjection) -> _StoredActiveProjection:
    match projection:
        case CompletionExpectedAt(epoch_seconds):
            return epoch_seconds
        case CompletionUnmeasured():
            return UNMEASURED
        case ProjectionAnswerOwed():
            return None


def _stored_projection(projection: Projection) -> _StoredProjection:
    if isinstance(projection, ZeroNoticeOwed):
        if isinstance(projection.resumed_projection, ProjectionNotRequested):
            return "zero-notice-owed"
        return {"zero-notice-owed": _stored_active_projection(projection.resumed_projection)}
    return _stored_active_projection(projection)


def _record_projection(saved: dict[str, Projection], unit: str, answer: ActiveProjection) -> None:
    previous = saved.get(unit)
    saved[unit] = ZeroNoticeOwed(answer) if isinstance(previous, ZeroNoticeOwed) else answer


@contextmanager
def projections() -> Generator[dict[str, Projection]]:
    """The projections under their lock; the caller's changes are saved on exit."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        held = read_projections()
        yield held
        temporary = STATE / f"{PROJECTIONS}.tmp"
        stored = {unit: _stored_projection(projection) for unit, projection in held.items()}
        _ = temporary.write_text(json.dumps(stored), encoding="utf-8")
        _ = temporary.replace(STATE / PROJECTIONS)


def projected(unit: str, held: Mapping[str, Projection], now: float) -> str:
    if unit not in held:
        return "ETA Unmeasured"
    projection = held[unit]
    active = projection.resumed_projection if isinstance(projection, ZeroNoticeOwed) else projection
    match active:
        case ProjectionAnswerOwed():
            return "asked, answer owed"
        case CompletionUnmeasured() | ProjectionNotRequested():
            return "ETA Unmeasured"
        case CompletionExpectedAt(epoch_seconds) if epoch_seconds <= now:
            return "ETA Unmeasured"
        case CompletionExpectedAt(epoch_seconds):
            minutes = math.ceil((epoch_seconds - now) / 60)
            return f"likely finishes in {minutes} minute{'' if minutes == 1 else 's'}"


def render(heading: str, rows: Mapping[str, int], held: Mapping[str, Projection], now: float) -> str:
    lines = [f"{name} - {rows[name]}" + (f" - {projected(name, held, now)}" if rows[name] else "")
             for name in sorted(rows, key=str.casefold)]
    return "\n".join([heading, "", "```", *lines, "```"])


def clock(zone: str) -> str:
    return datetime.now(ZoneInfo(zone)).strftime("%H:%M %Z")


def needs_asking(unit: str, held: Mapping[str, Projection], now: float) -> bool:
    if unit not in held:
        return True
    projection = held[unit]
    active = projection.resumed_projection if isinstance(projection, ZeroNoticeOwed) else projection
    match active:
        case CompletionExpectedAt(epoch_seconds):
            return epoch_seconds <= now
        case CompletionUnmeasured() | ProjectionAnswerOwed():
            return False
        case ProjectionNotRequested():
            return True


def ask(showrunner: str, unit: str, agents: int) -> bool:
    return not broadcast.send(unit, showrunner, ASK.format(showrunner=showrunner, agents=agents,
                                                           script=Path(__file__).resolve(), unit=shlex.quote(unit)))


def refresh(showrunner: str, units: list[str], rows: Mapping[str, int], now: float) -> dict[str, Projection]:
    """Ask each unit whose running agents have no current projection, and tell each one that reached none."""
    held = read_projections()
    asked = [unit for unit in units
             if rows[unit] and needs_asking(unit, held, now) and ask(showrunner, unit, rows[unit])]
    with projections() as saved:
        owed: dict[str, ZeroNoticeOwed] = {}
        for unit in units:
            if saved.get(unit) != held.get(unit):
                continue
            if not rows[unit] and unit in saved:
                saved[unit] = ZeroNoticeOwed()
            elif unit in asked:
                _record_projection(saved, unit, ProjectionAnswerOwed())
            notice = saved.get(unit)
            if isinstance(notice, ZeroNoticeOwed):
                owed[unit] = notice
        current = dict(saved)
    for unit, observed_notice in owed.items():
        with projections() as saved:
            notice = saved.get(unit)
            if (isinstance(notice, ZeroNoticeOwed)
                    and notice == observed_notice
                    and not broadcast.send(unit, showrunner, AT_ZERO.format(showrunner=showrunner))):
                if isinstance(notice.resumed_projection, ProjectionNotRequested):
                    del saved[unit]
                else:
                    saved[unit] = notice.resumed_projection
            current = dict(saved)
    return current


@contextmanager
def quiet_notice_state() -> Generator[QuietNoticeState]:
    """Serialize count observations and quiet marker changes, without holding a send."""
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / f"{QUIET}.state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            generation, status = (STATE / f"{QUIET}.state").read_text(encoding="utf-8").split()
            state = QuietNoticeState(int(generation), sent=status == "sent")
        except FileNotFoundError:
            try:
                sent = (STATE / QUIET).read_text(encoding="utf-8") == "sent\n"
            except FileNotFoundError:
                sent = False
            state = QuietNoticeState(0, sent)
        yield state


def _save_quiet_notice_state(state: QuietNoticeState) -> None:
    """Save the state while the caller holds the quiet state lock."""
    temporary = STATE / f"{QUIET}.state.tmp"
    _ = temporary.write_text(f"{state.generation} {'sent' if state.sent else 'owed'}\n", encoding="utf-8")
    _ = temporary.replace(STATE / f"{QUIET}.state")


def _cancel_quiet_notice(state: QuietNoticeState) -> QuietNoticeState:
    """Invalidate earlier observations while the caller holds the quiet state lock."""
    cancelled = QuietNoticeState(state.generation + 1, sent=False)
    _save_quiet_notice_state(cancelled)
    (STATE / QUIET).unlink(missing_ok=True)
    return cancelled


def _observe_quiet_count(running: int, state: QuietNoticeState) -> QuietCountObservation:
    """Record the count while the caller holds the quiet state lock."""
    if running:
        state = _cancel_quiet_notice(state)
    return QuietCountObservation(running, state.generation)


def _finish_quiet_notice(observation: QuietCountObservation, sent: bool) -> None:
    with quiet_notice_state() as state:
        if observation.generation != state.generation:
            return
        _save_quiet_notice_state(QuietNoticeState(state.generation, sent))
        if sent:
            _ = (STATE / QUIET).write_text("sent\n", encoding="utf-8")
        else:
            (STATE / QUIET).unlink(missing_ok=True)


def announce_quiet(running: int | QuietCountObservation) -> None:
    """Tell the user once when a wind-down has no Codex agent left on the machine; a later agent re-arms it."""
    if isinstance(running, QuietCountObservation):
        observation = running
    else:
        with quiet_notice_state() as state:
            observation = _observe_quiet_count(running, state)
    if observation.running:
        return
    notice = STATE / QUIET
    if not instances():
        with quiet_notice_state() as state:
            _ = _cancel_quiet_notice(state)
        return
    with (STATE / f"{QUIET}.lock").open("a") as delivery_lock:
        try:
            fcntl.flock(delivery_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with quiet_notice_state() as state:
            if observation.generation != state.generation:
                return
            if state.sent:
                _ = notice.write_text("sent\n", encoding="utf-8")
                return
        machine = socket.gethostname()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
        from push_texts import render, user_send_argv
        import review

        push = render("codex.reset_ready", machine=machine)
        command = user_send_argv(push)
        command[1] = str(broadcast.SEND)
        with quiet_notice_state() as state:
            if observation.generation != state.generation:
                return
            _ = notice.write_text("claim\n", encoding="utf-8")
        try:
            done = subprocess.run(command, capture_output=True, text=True, check=False,
                                  timeout=review.user_send_seconds() + 15)
            _finish_quiet_notice(observation, sent=not done.returncode)
            if done.returncode:
                print(f"codex wind-down: reset alert failed: {done.stdout.strip() or done.stderr.strip()}", file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            _finish_quiet_notice(observation, sent=False)
            print(f"codex wind-down: reset alert failed: {error}", file=sys.stderr)


def count(session: str) -> int:
    try:
        runner = showrunners.named(session)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    units = live_unit_names(runner)
    with quiet_notice_state() as state:
        counts = current_counts()
        observation = _observe_quiet_count(counts.total(), state)
    rows = listed(session, units, counts)
    held = refresh(session, units, rows, time.time())
    announce_quiet(observation)
    print(render(f"Codex agents, {clock(runner['zone'])}", rows, held, time.time()))
    return 0


def record_eta(unit: str, answer: str) -> int:
    if answer != UNMEASURED and not (answer.isascii() and answer.isdigit()):
        print(USAGE, file=sys.stderr)
        return 2
    with projections() as held:
        answer_projection = (
            CompletionUnmeasured()
            if answer == UNMEASURED
            else CompletionExpectedAt(time.time() + 60 * int(answer))
        )
        _record_projection(held, unit, answer_projection)
    print(f"{unit}: recorded")
    return 0


def instances() -> list[str]:
    return sorted(path.name for path in showrunners.NOTIFIER_STATE_DIR.glob(f"{INSTANCE}*") if path.is_dir())


def status() -> int:
    counts = current_counts()
    rows: dict[str, int] = {name: number for name, number in counts.items() if number}
    for runner in showrunners.registered_showrunners():
        rows.update(listed(runner["session"] or runner["slug"], live_unit_names(runner), counts))
    counting = ", ".join(name.removeprefix(INSTANCE) for name in instances())
    print(f"Wind-down: {'on for ' + counting if counting else 'off'}")
    print(render("Codex agents on this machine", rows, read_projections(), time.time()))
    return 0


def instance_name(session: str) -> str:
    return INSTANCE + re.sub(r"[^A-Za-z0-9._-]", "-", session)


def announce(sender: str, showrunner: str, unit: str) -> bool:
    """Send every showrunner and unit director its version at once and print each delivery."""
    zones = {runner["session"]: runner["zone"] for runner in showrunners.registered_showrunners()}
    named = {"sender": sender, "time": clock(zones.get(sender) or next(iter(zones.values()), "UTC")),
             "minutes": EVERY_MINUTES}
    return broadcast.report(broadcast.broadcast(sender, {
        broadcast.Showrunners(): showrunner.format(**named),
        broadcast.UnitDirectors(): unit.format(**named),
    }))


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
    (STATE / PROJECTIONS).unlink(missing_ok=True)
    with quiet_notice_state() as state:
        _ = _cancel_quiet_notice(state)
    every = announce(sender, WIND_DOWN, UNIT_WIND_DOWN)
    live: dict[str, SessionRecord] = {}
    for record in live_sessions():
        if record["name"]:
            _ = live.setdefault(record["name"], record)
    for runner in showrunners.registered_showrunners():
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
    return 0 if announce(sender, TRIAGE, TRIAGE) else 1


def clear(sender: str) -> int:
    every = True
    for name in instances():
        done = subprocess.run(["zsh", str(NOTIFIER), "remove", name], capture_output=True, text=True, check=False)
        (STATE / f"{name}.txt").unlink(missing_ok=True)
        every = every and done.returncode == 0
        print(f"{name.removeprefix(INSTANCE)}: {'count stopped' if done.returncode == 0 else 'count NOT stopped'}")
    (STATE / PROJECTIONS).unlink(missing_ok=True)
    with quiet_notice_state() as state:
        _ = _cancel_quiet_notice(state)
    return 0 if announce(sender, ALL_CLEAR, UNIT_CLEAR) and every else 1


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
