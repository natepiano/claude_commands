#!/usr/bin/env python3
"""Sample production waits and perform the mechanical work that clears them."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, NamedTuple, TypeVar, cast

from add_unit import Production, Refusal, cell_value, read_production, unit_rows
from merge_checkpoint import MergeEntry, NoMerge, Stop, git, merge_branch_history, report as merge_report

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


class CleanupClean(NamedTuple):
    pass


class CleanupFailed(NamedTuple):
    detail: str


class HoldQuotaAlert(NamedTuple):
    account: str
    alert: str


class ClearQuotaAlerts(NamedTuple):
    accounts: tuple[str, ...]


class RestoreQuotaAlerts(NamedTuple):
    accounts: tuple[str, ...]
    tools: tuple[str, ...]


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
    session: str
    branch: str
    worktree: Path


class WaitingFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step


def one_line(text: str) -> str:
    return "; ".join(line.strip() for line in text.splitlines() if line.strip())


def report(step: str, state: str, message: str) -> None:
    merge_report(step, state, one_line(message) if state == "failed" else message)


def units_from_doc(production: Production) -> tuple[Unit, ...]:
    _, rows = unit_rows(production.doc.read_text(encoding="utf-8").splitlines())
    units: list[Unit] = []
    for row in rows:
        cells = [cell_value(cell.strip()) for cell in row.strip("|").split("|")]
        if len(cells) < 5:
            raise WaitingFailure("production", f"invalid Units row: {row}")
        units.append(Unit(cells[0], cells[4], cells[3], Path(cells[2]).expanduser()))
    return tuple(units)


def named_unit(production: Production, name: str) -> Unit:
    for unit in units_from_doc(production):
        if name in (unit.name, unit.session):
            return unit
    raise WaitingFailure("production", f"unknown unit {name}")


def run_command(step: str, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise WaitingFailure(step, str(error)) from error


def detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"


def read_state(path: Path) -> JsonMap:
    if not path.exists():
        return {}
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise WaitingFailure("state", f"invalid {path}")
    return cast(JsonMap, raw)


def update_state(path: Path, change: Callable[[JsonMap], StateResult]) -> StateResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{path}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        content = read_state(path)
        before = json.dumps(content, sort_keys=True)
        result = change(content)
        if json.dumps(content, sort_keys=True) == before:
            return result
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                             prefix=f".{path.name}.", suffix=".tmp",
                                             delete=False) as temporary:
                temporary_name = temporary.name
                _ = temporary.write(json.dumps(content, indent=2) + "\n")
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
        return result


def eta_requested(seen: JsonMap, key: str) -> EtaRequested | EtaNotYetRequested:
    prior = seen.get(key)
    if isinstance(prior, dict) and cast(JsonMap, prior).get("requested") is True:
        return EtaRequested()
    return EtaNotYetRequested()


def eta_request(production: Production, name: str, phase: str, state_dir: Path) -> None:
    unit = named_unit(production, name)
    path = state_dir / "eta_seen.json"
    key = f"{unit.session}|{phase}"

    def request(seen: JsonMap) -> EtaRequested | EtaNotYetRequested:
        state = eta_requested(seen, key)
        if isinstance(state, EtaNotYetRequested):
            seen[key] = {"requested": True}
        return state

    state = update_state(path, request)
    if isinstance(state, EtaNotYetRequested):
        print(f"request /unit:eta: {unit.session}")
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
    cleanup: CleanupClean | CleanupFailed = CleanupClean()
    with tempfile.TemporaryDirectory(prefix="production-scratch-") as parent:
        worktree = Path(parent) / "checkout"
        added = False
        try:
            add = git(production.checkout, "worktree", "add", "--detach", str(worktree), unit.branch)
            if add.returncode:
                raise WaitingFailure("scratch-test", detail(add))
            added = True
            merge = git(worktree, "merge", "--no-commit", "--no-ff", production.merge_branch)
            if merge.returncode:
                _ = log.write_text(merge.stdout + merge.stderr, encoding="utf-8")
                state: ScratchGreen | ScratchRed | ScratchConflict = ScratchConflict(
                    f"{detail(merge)} ({log})")
            else:
                try:
                    with log.open("w", encoding="utf-8") as output:
                        test = subprocess.run(["sh", "-c", tests], cwd=worktree, stdout=output,
                                              stderr=subprocess.STDOUT, text=True, check=False)
                    tail = tail_lines(log, 4) or f"exit {test.returncode}"
                    state = ScratchGreen(log) if test.returncode == 0 else ScratchRed(tail, log)
                except OSError as error:
                    _ = log.write_text(f"{error}\n", encoding="utf-8")
                    state = ScratchRed(str(error), log)
        finally:
            if added:
                _ = git(worktree, "merge", "--abort")
                removed = git(production.checkout, "worktree", "remove", "--force", str(worktree))
                if removed.returncode:
                    cleanup = CleanupFailed(detail(removed))
    passed = scratch_verdict(state)
    if isinstance(cleanup, CleanupFailed):
        report("scratch-test", "failed", f"temporary worktree cleanup: {cleanup.detail}")
    if not passed or isinstance(cleanup, CleanupFailed):
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
    active: dict[str, WaitOpen | WaitCleared] = {}
    clears: dict[str, str] = {}
    for event in log_events(production, now):
        opened = re.search(r": block: (.+?) on (.+?) \((?:code|files): .*\), clears ~([^ ]+)", event.line)
        closed = re.search(r": block cleared: (.+?) on (.+)$", event.line)
        if opened:
            key = f"{opened.group(1)} on {opened.group(2)}"
            active[key], clears[key] = WaitOpen(event.moment), opened.group(3)
        elif closed:
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


def phase_start(events: tuple[LogEvent, ...], unit: Unit,
                previous: MergeEntry | NoMerge) -> PhaseStart | NoPhaseStart:
    if not isinstance(previous, NoMerge):
        prior_phase = str(previous.phase)
        pattern = re.compile(rf"(?:^|: ){re.escape(unit.name)} phase {re.escape(prior_phase)} "
                             + r"\([^)]+\) merged as [^;]+;")
    else:
        pattern = re.compile(rf": added {re.escape(unit.name)} \([^)]+\), tmux "
                             + rf"{re.escape(unit.session)}, worktree ")
    for event in reversed(events):
        if pattern.search(event.line):
            return PhaseStart(event.moment)
    return NoPhaseStart()


def eta_moment(text: str, line_moment: datetime) -> EtaOnLog | NoEtaOnLog:
    weekday = re.match(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d\d):(\d\d) [A-Z]{3}(?:,|$)", text)
    if weekday is not None:
        target = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun").index(weekday.group(1))
        days = (target - line_moment.weekday()) % 7
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
    labels = tuple(dict.fromkeys((unit.session, unit.name)))
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
    history = merge_branch_history(production.checkout, production.merge_branch)
    due_items: list[AgendaOpen] = []
    for unit in units_from_doc(production):
        phase = current_phase(events, unit)
        if isinstance(phase, NoPhaseOnLog):
            continue
        start = phase_start(events, unit, history.last_code_for_unit(unit.name))
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
        return tuple(opened)

    opened = update_state(state_dir / "agenda_seen.json", open_items)
    report("agenda", "ok", f"{len(opened)} new items")
    for item in opened:
        print(f"agenda: ok — {item.unit}|{item.phase}")


def quota(production: Production, notice: str, state_dir: Path) -> None:
    lines = notice.strip().splitlines()
    line = lines[0] if lines else ""
    kind = next((prefix for prefix in ("Quota alert acknowledged:", "Quota restored:", "Quota alert:")
                 if line.startswith(prefix)), "")
    if not kind:
        raise WaitingFailure("quota", "expected a quota alert notice")
    path = state_dir / "quota_seen.json"
    if kind == "Quota alert:":
        named_account = re.match(r"Quota alert: (.+?), the active (\w+) account,", line)
        if named_account is None:
            raise WaitingFailure("quota", "expected a quota alert account line")
        account, tool = named_account.group(1), named_account.group(2)
        change: HoldQuotaAlert | ClearQuotaAlerts | RestoreQuotaAlerts = HoldQuotaAlert(account, line)
        action = (f"delegation continues on Claude; rerun {tool} work in flight"
                  if "Every function that ran on" in notice
                  else f"start no new delegate work on {tool}; running seats finish")
    elif kind == "Quota alert acknowledged:":
        named = re.search(r"the user silenced (.+?) (?:in session|outside any session)", line)
        accounts = [item.strip() for item in named.group(1).split(",")] if named else [line.removeprefix(kind).strip()]
        change = ClearQuotaAlerts(tuple(accounts))
        action = "keep delegation paused on the acknowledged account"
    else:
        named_accounts = [match.group(1) for entry in lines[1:]
                          if (match := re.match(r"(.+?), the active \w+ account", entry))]
        named_tools = re.search(r"delegation on (.+?) can resume", line)
        tools = named_tools.group(1) if named_tools else line.removeprefix(kind).strip()
        change = RestoreQuotaAlerts(tuple(named_accounts), tuple(item.strip().lower()
                                                                 for item in tools.split(" and ")))
        action = f"delegation on {tools} can resume"
    sender = shutil.which("send.py") or str(Path(__file__).resolve().parent.parent / "message/send.py")
    failures: list[str] = []
    for unit in units_from_doc(production):
        try:
            delivered = subprocess.run([sys.executable, sender, "--to", unit.session,
                                        "--from", production.showrunner_session,
                                        "--text", f"From the showrunner: {action}"],
                                       capture_output=True, text=True, check=False)
        except OSError as error:
            failures.append(f"{unit.session}: {error}")
            continue
        if delivered.returncode == 0:
            print(f"send {unit.session}: From the showrunner: {action}")
        elif delivered.returncode == 1:
            print(f"queued {unit.session}: From the showrunner: {action}")
        else:
            failures.append(f"{unit.session}: {detail(delivered)}")

    def change_held(held: JsonMap) -> JsonMap:
        if isinstance(change, HoldQuotaAlert):
            held[change.account] = change.alert
        elif isinstance(change, ClearQuotaAlerts):
            for account in change.accounts:
                _ = held.pop(account, None)
        else:
            named_held = any(account in held for account in change.accounts)
            for account in change.accounts:
                _ = held.pop(account, None)
            if not named_held:
                for account, alert in tuple(held.items()):
                    alert_text = alert if isinstance(alert, str) else ""
                    active_tool = re.search(r"the active (\w+) account", alert_text)
                    tool = active_tool.group(1).lower() if active_tool else ""
                    if tool in change.tools:
                        _ = held.pop(account, None)
        return held

    held = update_state(path, change_held)
    if failures:
        report("quota", "failed", "; ".join(failures))
        raise WaitingFailure("quota", "")
    report("quota", "ok", f"{len(held)} held account alerts")
    for alert in held.values():
        print(f"held: {alert}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="step", required=True)
    for name in ("scratch-test", "waits", "search", "quota", "agenda", "eta-request"):
        sub = commands.add_parser(name)
        _ = sub.add_argument("--production", required=True, type=Path)
        if name in ("scratch-test", "quota", "agenda", "eta-request"):
            _ = sub.add_argument("--state-dir", required=True, type=Path)
        if name == "scratch-test":
            _ = sub.add_argument("unit")
            _ = sub.add_argument("--tests", required=True)
        elif name == "search":
            _ = sub.add_argument("old")
            _ = sub.add_argument("new")
        elif name == "quota":
            _ = sub.add_argument("--notice")
        elif name == "eta-request":
            _ = sub.add_argument("unit")
            _ = sub.add_argument("--phase", required=True)
    args = parser.parse_args()
    step = cast(str, args.step)
    try:
        production = read_production(cast(Path, args.production))
        if step == "scratch-test":
            scratch_test(production, cast(str, args.unit), cast(str, args.tests), cast(Path, args.state_dir))
        elif step == "waits":
            waits(production)
        elif step == "search":
            search(production, cast(str, args.old), cast(str, args.new))
        elif step == "quota":
            quota(production, cast(str | None, args.notice) or sys.stdin.read(), cast(Path, args.state_dir))
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
