#!/usr/bin/env python3
"""Sample production waits and perform the mechanical work that clears them."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, NamedTuple, TypeVar, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.copies import copies
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail

from scripts.production import session_staff, staff, unit_lookup
from scripts.production.production_doc import Production, Refusal, read_production

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "whoami"))
from scripts.production.merge_checkpoint import Stop, git, report as merge_report
from scripts.direct import progress_history
from scripts.lib.user_zone import user_zone

JsonMap = dict[str, object]
StateResult = TypeVar("StateResult")


class WaitOpen(NamedTuple):
    since: datetime


class WaitCleared(NamedTuple):
    pass


class EtaRequested(NamedTuple):
    pass


class EtaNotYetRequested(NamedTuple):
    pass


class ScratchGreen(NamedTuple):
    log: Path


class ScratchRed(NamedTuple):
    last_lines: str
    log: Path


class ScratchConflict(NamedTuple):
    detail: str


class AgendaOpen(NamedTuple):
    unit: str
    phase: str


class AgendaNotDue(NamedTuple):
    pass


class PhaseOnLog(NamedTuple):
    phase: str


class NoPhaseOnLog(NamedTuple):
    pass


class EtaOnLog(NamedTuple):
    moment: datetime


class NoEtaOnLog(NamedTuple):
    pass


class PhaseStart(NamedTuple):
    moment: datetime


class NoPhaseStart(NamedTuple):
    pass


class EventAt(NamedTuple):
    moment: datetime


class EventWithoutTime(NamedTuple):
    pass


class LogEvent(NamedTuple):
    line: str
    moment: datetime


class BerthOverlap(NamedTuple):
    scopes: tuple[str, ...]
    blocker: str
    reason: str


class BerthConfigured(NamedTuple):
    overlaps: tuple[BerthOverlap, ...]


class BerthNotConfigured(NamedTuple):
    pass


class Unit(NamedTuple):
    name: str
    # The unit's session name at this moment, read from its live session; empty when none runs.
    # The session addresses the unit. The fixed staff id keys saved state.
    session: str
    branch: str
    worktree: Path
    # The pid of the unit's live Claude; None when none runs.
    pid: int | None
    staff_id: staff.StaffId


class WaitingFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step


def one_line(text: str) -> str:
    return "; ".join(line.strip() for line in text.splitlines() if line.strip())


def report(step: str, state: str, message: str) -> None:
    merge_report(step, state, one_line(message) if state == "failed" else message)


def units_from_doc(production: Production) -> tuple[Unit, ...]:
    """The active units in staff row order; a unit with no readable folder or branch is reported and left out."""
    try:
        record = staff.snapshot()
        rows = record.find(kind="unit", parent=production.owner.id)
        marked = unit_lookup.labeled_sessions(production.name, record=record)
    except (OSError, staff.StaffRefused) as error:
        raise WaitingFailure("production", f"the unit sessions could not be looked up: {error}") from error
    resolver = session_staff.RowResolver(record)
    units: list[Unit] = []
    for row in rows:
        name = row.name
        if isinstance(row.worktree, staff.WorktreeUnfilled):
            print(f"waiting: {name}: unavailable: the worktree is not recorded", file=sys.stderr)
        if not isinstance(row.worktree, staff.PresentWorktree):
            continue
        branch = resolver.unit_branch(row)
        if not isinstance(branch, session_staff.Branch):
            print(f"waiting: {name}: unavailable: {branch.reason}", file=sys.stderr)
            continue
        found = marked.get(row.name)
        claude = found.claude if found is not None else None
        live = isinstance(claude, unit_lookup.LiveClaude)
        units.append(Unit(name, claude.name if live else "", branch.name, row.worktree.path,
                          claude.pid if live else None, row.id))
    return tuple(units)


def named_unit(production: Production, name: str) -> Unit:
    for unit in units_from_doc(production):
        if name and name in (unit.name, unit.session):
            return unit
    raise WaitingFailure("production", f"unknown unit {name}")


def run_command(step: str, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise WaitingFailure(step, str(error)) from error


def detail(result: subprocess.CompletedProcess[str]) -> str:
    return failure_detail(result)


def read_state(path: Path) -> JsonMap:
    if not path.exists():
        return {}
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise WaitingFailure("state", f"invalid {path}")
    return cast(JsonMap, raw)


def update_state(path: Path, change: Callable[[JsonMap], StateResult], *,
                 read: Callable[[Path], JsonMap] = read_state) -> StateResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{path}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        content = read(path)
        before = json.dumps(content, sort_keys=True)
        result = change(content)
        if json.dumps(content, sort_keys=True) == before:
            return result
        write_text_atomic(path, json.dumps(content, indent=2) + "\n")
        return result


def eta_requested(seen: JsonMap, key: str) -> EtaRequested | EtaNotYetRequested:
    prior = seen.get(key)
    if isinstance(prior, dict) and cast(JsonMap, prior).get("requested") is True:
        return EtaRequested()
    return EtaNotYetRequested()


def eta_request(production: Production, name: str, phase: str, state_dir: Path) -> None:
    unit = named_unit(production, name)
    path = state_dir / "eta_seen.json"
    key = f"{unit.staff_id}|{phase}"

    def request(seen: JsonMap) -> EtaRequested | EtaNotYetRequested:
        state = eta_requested(seen, key)
        if isinstance(state, EtaNotYetRequested):
            print(f"request /unit:eta: {unit.name}", flush=True)
            seen[key] = {"requested": True}
        return state

    state = update_state(path, request)
    report("eta-request", "ok", "already requested" if isinstance(state, EtaRequested) else "requested")


def tail_lines(path: Path, count: int) -> str:
    with path.open("rb") as output:
        _ = output.seek(0, os.SEEK_END)
        position = output.tell()
        chunks: list[bytes] = []
        newlines = 0
        while position > 0 and newlines <= count:
            size = min(4096, position)
            position -= size
            _ = output.seek(position)
            chunk = output.read(size)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
    lines = [line for line in b"".join(reversed(chunks)).decode(
        "utf-8", errors="replace").strip().splitlines() if line.strip()]
    return "; ".join(lines[-count:])


def scratch_verdict(state: ScratchGreen | ScratchRed | ScratchConflict) -> bool:
    if isinstance(state, ScratchConflict):
        report("scratch-test", "failed", f"merge conflict: {state.detail}")
        return False
    if isinstance(state, ScratchRed):
        report("scratch-test", "failed", f"red: {state.last_lines} ({state.log})")
        return False
    report("scratch-test", "ok", f"green ({state.log})")
    return True


def scratch_test(production: Production, name: str, tests: str, state_dir: Path) -> None:
    unit = named_unit(production, name)
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(production.zone).strftime("%Y%m%d-%H%M%S-%f")
    log = state_dir / f"scratch-test-{unit.name}-{stamp}.log"
    passed = False
    try:
        with copies.standing(production.name, production.checkout, copies.CommittedSource(unit.branch),
                             merge=copies.MergeRevision(production.merge_branch)) as worktree:
            try:
                with log.open("w", encoding="utf-8") as output:
                    test = subprocess.run(["sh", "-c", tests], cwd=worktree, stdout=output,
                                          stderr=subprocess.STDOUT, text=True, check=False)
                tail = tail_lines(log, 4) or f"exit {test.returncode}"
                state: ScratchGreen | ScratchRed | ScratchConflict = (
                    ScratchGreen(log) if test.returncode == 0 else ScratchRed(tail, log))
            except OSError as error:
                _ = log.write_text(f"{error}\n", encoding="utf-8")
                state = ScratchRed(str(error), log)
            passed = scratch_verdict(state)
    except copies.PreparationError as error:
        _ = log.write_text(f"{error}\n", encoding="utf-8")
        _ = scratch_verdict(ScratchConflict(f"{error} ({log})"))
        raise WaitingFailure("scratch-test", "") from error
    except copies.PostUseError as error:
        raise WaitingFailure("scratch-test", f"post-use step: {error}") from error
    except copies.CopyError as error:
        raise WaitingFailure("scratch-test", str(error)) from error
    if not passed:
        raise WaitingFailure("scratch-test", "")


def search(production: Production, old: str, new: str) -> None:
    sites: list[str] = []
    for unit in units_from_doc(production):
        branch = git(production.checkout, "grep", "-n", "-F", old, unit.branch, "--")
        if branch.returncode not in (0, 1):
            raise WaitingFailure("search", detail(branch))
        sites.extend(f"{unit.name} branch {line}" for line in branch.stdout.splitlines())
        if unit.worktree.is_dir():
            live = run_command("search", ["rg", "-n", "-F", "--hidden", "--glob", "!.git", old,
                                          str(unit.worktree)])
            if live.returncode not in (0, 1):
                raise WaitingFailure("search", detail(live))
            sites.extend(f"{unit.name} worktree {line}" for line in live.stdout.splitlines())
    report("search", "ok", f"{len(sites)} sites for {old} → {new}")
    for site in sites:
        print(site)


def event_time(line: str, later: datetime) -> EventAt | EventWithoutTime:
    match = re.match(r"- (\d\d):(\d\d) [A-Z]{3}: ", line)
    if match is None:
        return EventWithoutTime()
    candidate = later.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
    # A gap over 24 hours between consecutive timestamped lines is invisible in this date-less format.
    return EventAt(candidate - timedelta(days=1) if candidate > later else candidate)


def log_events(production: Production, now: datetime) -> tuple[LogEvent, ...]:
    lines = production.log.read_text(encoding="utf-8").splitlines()
    reversed_events: list[LogEvent] = []
    later = now
    for line in reversed(lines):
        state = event_time(line, later)
        if isinstance(state, EventAt):
            later = state.moment
        reversed_events.append(LogEvent(line, later))
    return tuple(reversed(reversed_events))


def json_map(value: object, label: str) -> JsonMap:
    if not isinstance(value, dict):
        raise WaitingFailure("waits", f"board_ready: invalid {label}")
    return cast(JsonMap, value)


def berth_board(result: subprocess.CompletedProcess[str]) -> BerthConfigured | BerthNotConfigured:
    try:
        raw = cast(object, json.loads(result.stdout))
    except json.JSONDecodeError as error:
        output = one_line(result.stdout) or f"exit {result.returncode}"
        raise WaitingFailure("waits", f"{output}: {error.msg}") from error
    if not isinstance(raw, dict):
        output = one_line(result.stdout) or f"exit {result.returncode}"
        raise WaitingFailure("waits", f"{output}: expected a JSON object")
    board = cast(JsonMap, raw)
    status = board.get("status")
    status_text = status if isinstance(status, str) else one_line(result.stdout)
    message = board.get("message")
    message_text = message if isinstance(message, str) and message else detail(result)
    if status_text == "unconfigured":
        return BerthNotConfigured()
    if status_text != "board_ready":
        raise WaitingFailure("waits", f"{status_text or 'unknown status'}: {message_text}")
    payload = json_map(board.get("payload"), "payload")
    data = json_map(payload.get("data"), "payload.data")
    unresolved = json_map(data.get("unresolved_overlaps"), "payload.data.unresolved_overlaps")
    raw_entries = unresolved.get("entries")
    if not isinstance(raw_entries, list):
        raise WaitingFailure("waits", "board_ready: invalid payload.data.unresolved_overlaps.entries")
    overlaps: list[BerthOverlap] = []
    for raw_entry in cast(list[object], raw_entries):
        entry = json_map(raw_entry, "overlap entry")
        raw_scopes = entry.get("scopes")
        if not isinstance(raw_scopes, list):
            raise WaitingFailure("waits", "board_ready: invalid overlap scopes")
        scopes: list[str] = []
        for raw_scope in cast(list[object], raw_scopes):
            scope = json_map(raw_scope, "overlap scope")
            path = scope.get("path")
            if not isinstance(path, str):
                raise WaitingFailure("waits", "board_ready: invalid overlap scope path")
            scopes.append(path)
        blocker = entry.get("blocker")
        reason = entry.get("reason")
        if not isinstance(blocker, str) or not isinstance(reason, str):
            raise WaitingFailure("waits", "board_ready: invalid overlap blocker or reason")
        overlaps.append(BerthOverlap(tuple(scopes), blocker, reason))
    return BerthConfigured(tuple(overlaps))


def waits(production: Production) -> None:
    board = run_command("waits", ["cargo-berth", "board", "--json"])
    overlaps = berth_board(board)
    now = datetime.now(production.zone)
    retired = {row.name for row in staff.snapshot().find(kind="unit", parent=production.owner.id)
               if isinstance(row.worktree, staff.RemovedWorktree)}
    active: dict[str, WaitOpen | WaitCleared] = {}
    clears: dict[str, str] = {}
    for event in log_events(production, now):
        opened = re.search(r": block: (.+?) on (.+?) \((?:code|files): .*\), clears ~([^ ]+)", event.line)
        closed = re.search(r": block cleared: (.+?) on (.+)$", event.line)
        if opened and opened.group(1) not in retired:
            key = f"{opened.group(1)} on {opened.group(2)}"
            active[key], clears[key] = WaitOpen(event.moment), opened.group(3)
        elif closed and closed.group(1) not in retired:
            active[f"{closed.group(1)} on {closed.group(2)}"] = WaitCleared()
    open_count = sum(isinstance(value, WaitOpen) for value in active.values())
    if isinstance(overlaps, BerthNotConfigured):
        report("waits", "ok", f"{open_count} open; berth not configured")
    else:
        report("waits", "ok", f"{open_count} open; {len(overlaps.overlaps)} berth overlaps")
        for overlap in overlaps.overlaps:
            print(f"waits: ok — berth overlap: {', '.join(overlap.scopes)}; "
                  + f"blocker {overlap.blocker}; {overlap.reason}")
    for key, state in active.items():
        if isinstance(state, WaitOpen):
            age = int((now - state.since).total_seconds() // 60)
            clear = clears[key]
            match = re.fullmatch(r"(\d\d):(\d\d)", clear)
            late = False
            if match:
                due = state.since.replace(hour=int(match.group(1)), minute=int(match.group(2)))
                if due < state.since:
                    due += timedelta(days=1)
                late = now >= due + timedelta(minutes=30)
            print(f"waits: ok — {key}: open {age} minutes; clears ~{clear}; "
                  + ("act now" if age >= 60 or late else "within limit"))


def phase_identity(text: str) -> str:
    return text.split(";", 1)[0].split(":", 1)[0].strip()


def current_phase(events: tuple[LogEvent, ...], unit: Unit) -> PhaseOnLog | NoPhaseOnLog:
    state_index = -1
    for index, event in enumerate(events):
        if event.line == "### STATE" or event.line.startswith("### STATE "):
            state_index = index
    if state_index < 0:
        return NoPhaseOnLog()
    prefix = f"{unit.name}: "
    for event in events[state_index + 1:]:
        if event.line.startswith("- ") or event.line.startswith("### "):
            break
        if event.line.startswith(prefix):
            phase = event.line.removeprefix(prefix).split(";", 1)[0].strip()
            if phase.startswith(("Phase ", "follow-up ")):
                return PhaseOnLog(phase_identity(phase))
            return NoPhaseOnLog()
    return NoPhaseOnLog()


def phase_start(unit: Unit, phase: str, events: list[dict[str, object]]) -> PhaseStart | NoPhaseStart:
    starts = [event["timestamp_epoch"] for event in events
              if event.get("working_dir") == str(unit.worktree.resolve()) and event.get("phase_id") == phase
              and isinstance(event.get("timestamp_epoch"), (int, float))]
    return (PhaseStart(datetime.fromtimestamp(float(cast(float, starts[-1])), user_zone()))
            if starts else NoPhaseStart())


def eta_moment(text: str, line_moment: datetime) -> EtaOnLog | NoEtaOnLog:
    weekday = re.match(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d\d):(\d\d) [A-Z]{3}(?:,|$)", text)
    if weekday is not None:
        target = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun").index(weekday.group(1))
        days = (target - line_moment.weekday()) % 7
        if days > 3:
            days -= 7
        moment = line_moment.replace(hour=int(weekday.group(2)), minute=int(weekday.group(3)),
                                     second=0, microsecond=0) + timedelta(days=days)
        return EtaOnLog(moment)
    clock = re.match(r"(\d\d):(\d\d) [A-Z]{3}( tomorrow)?(?:,|$)", text)
    if clock is None:
        return NoEtaOnLog()
    moment = line_moment.replace(hour=int(clock.group(1)), minute=int(clock.group(2)),
                                 second=0, microsecond=0)
    if clock.group(3):
        moment += timedelta(days=1)
    return EtaOnLog(moment)


def phase_eta(events: tuple[LogEvent, ...], unit: Unit, phase: str,
              start: PhaseStart) -> EtaOnLog | NoEtaOnLog:
    labels = tuple(label for label in dict.fromkeys((unit.session, unit.name)) if label)
    for event in reversed(events):
        if event.moment < start.moment or "dailies ETAs:" not in event.line:
            continue
        segments = event.line.split("dailies ETAs:", 1)[1].strip().split("; ")
        for segment in segments:
            for label in labels:
                prefix = f"{label} {phase} "
                if segment.startswith(prefix):
                    return eta_moment(segment.removeprefix(prefix), event.moment)
    return NoEtaOnLog()


def agenda_state(seen: JsonMap, item: AgendaOpen) -> AgendaOpen | AgendaNotDue:
    if f"{item.unit}|{item.phase}" in seen:
        return AgendaNotDue()
    return item


def agenda(production: Production, state_dir: Path) -> None:
    now = datetime.now(production.zone)
    events = log_events(production, now)
    due_items: list[AgendaOpen] = []
    units = units_from_doc(production)
    starts = progress_history.events_for_worktrees(tuple(unit.worktree for unit in units), frozenset({"phase_started"}))
    for unit in units:
        phase = current_phase(events, unit)
        if isinstance(phase, NoPhaseOnLog):
            continue
        start = phase_start(unit, re.sub(r"^Phase (\S+).*", r"\1", phase.phase), starts)
        if isinstance(start, NoPhaseStart):
            continue
        eta = phase_eta(events, unit, phase.phase, start)
        due = now - start.moment > timedelta(hours=8)
        if isinstance(eta, EtaOnLog):
            due = due or eta.moment - start.moment > timedelta(hours=8)
        if due:
            due_items.append(AgendaOpen(unit.name, phase.phase))

    def open_items(seen: JsonMap) -> tuple[AgendaOpen, ...]:
        opened: list[AgendaOpen] = []
        for item in due_items:
            state = agenda_state(seen, item)
            if isinstance(state, AgendaOpen):
                seen[f"{state.unit}|{state.phase}"] = True
                opened.append(state)
        report("agenda", "ok", f"{len(opened)} new items")
        for item in opened:
            print(f"agenda: ok — {item.unit}|{item.phase}", flush=True)
        return tuple(opened)

    _ = update_state(state_dir / "agenda_seen.json", open_items)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="step", required=True)
    for name in ("scratch-test", "waits", "search", "agenda", "eta-request"):
        sub = commands.add_parser(name)
        _ = sub.add_argument("--production", required=True)
        if name in ("scratch-test", "agenda", "eta-request"):
            _ = sub.add_argument("--state-dir", required=True, type=Path)
        if name == "scratch-test":
            _ = sub.add_argument("unit")
            _ = sub.add_argument("--tests", required=True)
        elif name == "search":
            _ = sub.add_argument("old")
            _ = sub.add_argument("new")
        elif name == "eta-request":
            _ = sub.add_argument("unit")
            _ = sub.add_argument("--phase", required=True)
    args = parser.parse_args()
    step = cast(str, args.step)
    try:
        production = read_production(cast(str, args.production))
        if step == "scratch-test":
            scratch_test(production, cast(str, args.unit), cast(str, args.tests), cast(Path, args.state_dir))
        elif step == "waits":
            waits(production)
        elif step == "search":
            search(production, cast(str, args.old), cast(str, args.new))
        elif step == "agenda":
            agenda(production, cast(Path, args.state_dir))
        else:
            eta_request(production, cast(str, args.unit), cast(str, args.phase), cast(Path, args.state_dir))
        return 0
    except Stop as error:
        report(step, "failed", error.detail)
        return 2
    except (WaitingFailure, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        if str(error):
            report(error.step if isinstance(error, WaitingFailure) else step, "failed", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
