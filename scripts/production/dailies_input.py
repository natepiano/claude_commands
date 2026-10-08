#!/usr/bin/env python3
"""Build the mechanical dailies input around the showrunner's judgment JSON."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import NamedTuple, cast
from zoneinfo import ZoneInfo

import unit_lookup
from add_unit import Refusal, cell_value, live_unit_table, read_production, unit_table
from ci_points import PointFailure, WatchFirstAlert, WatchRepeat, review_watch
from dailies_render import InputError, StateRefused, as_list, as_map, check_render_state, local_now, parse_report, parse_time
from merge_checkpoint import NoMerge, git, merge_branch_history
from waiting import EtaNotYetRequested, EtaRequested, eta_requested, update_state

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "build_hold"))
from build_hold import ActiveHolders, read_holders

JsonMap = dict[str, object]


class SessionGone(NamedTuple):
    pass


class ClaudeNotRunning(NamedTuple):
    pass


class Running(NamedTuple):
    pass


class FormWaiting(NamedTuple):
    text: str


class Decision(NamedTuple):
    text: str


class StillWaiting(NamedTuple):
    text: str


class Block(NamedTuple):
    age: str
    text: str


class TicksFailing(NamedTuple):
    text: str


class Activity(NamedTuple):
    text: str


Flag = FormWaiting | Decision | StillWaiting | Block | TicksFailing
SessionState = SessionGone | ClaudeNotRunning | Running


class StatusBlock(NamedTuple):
    session: str
    state: SessionState
    flags: tuple[Flag, ...]
    activity: tuple[Activity, ...]


class EtaFresh(NamedTuple):
    text: str


class EtaStale(NamedTuple):
    text: str
    first_seen: datetime


class EtaPassed(NamedTuple):
    pass


class EtaNone(NamedTuple):
    pass


EtaState = EtaFresh | EtaStale | EtaPassed | EtaNone


class UnitRow(NamedTuple):
    name: str
    # The unit's session name at this moment, read from its live session; empty when none runs.
    # It addresses the unit and is never a key: every record is kept under the unit's name.
    session: str


class DailiesFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.detail: str = detail


def report(step: str, state: str, detail: str) -> None:
    print(f"{step}: {state} — {detail.replace(chr(10), '; ')}", flush=True)


def units_from_doc(lines: list[str], slug: str) -> tuple[UnitRow, ...]:
    live_rows = live_unit_table(lines, slug)
    if unit_table(lines) and not live_rows:
        raise DailiesFailure("production", "no live units: every Units row is retired")
    try:
        marked = unit_lookup.marked_units(slug)
    except OSError as error:
        raise DailiesFailure("production", f"the unit sessions could not be looked up: {error}") from error
    units: list[UnitRow] = []
    for cells in live_rows:
        name = cell_value(cells.get("Unit", ""))
        if not name:
            raise DailiesFailure("production", f"invalid Units row: {cells}")
        found = marked.get(name)
        claude = found.claude if found is not None else None
        units.append(UnitRow(name, claude.name if isinstance(claude, unit_lookup.LiveClaude) else ""))
    return tuple(units)


def status_blocks(path: Path, units: tuple[UnitRow, ...]) -> tuple[StatusBlock, ...]:
    names = {unit.name for unit in units}
    blocks: list[StatusBlock] = []
    session = ""
    state: SessionState = Running()
    flags: list[Flag] = []
    activity: list[Activity] = []
    decision: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("== "):
            if session:
                if decision is not None:
                    raise DailiesFailure("status", f"{session}: unclosed DECISION block")
                blocks.append(StatusBlock(session, state, tuple(flags), tuple(activity)))
            session = line[3:].strip()
            if session not in names:
                raise DailiesFailure("status", f"unknown unit {session}")
            if any(block.session == session for block in blocks):
                raise DailiesFailure("status", f"duplicate unit {session}")
            state, flags, activity, decision = Running(), [], [], None
            continue
        if not session:
            if not re.fullmatch(r"at .+ / \d\d:\d\d UTC", line):
                raise DailiesFailure("status", f"before first unit: {line}")
            continue
        if decision is not None:
            if line == f"=== end {session} ===":
                flags.append(Decision("\n".join(decision)))
                decision = None
            else:
                decision.append(line)
            continue
        if line == f"=== DECISION for you from {session} ===":
            decision = []
        elif line == "SESSION GONE":
            state = SessionGone()
        elif line == "CLAUDE NOT RUNNING":
            state = ClaudeNotRunning()
        elif line.startswith("TICKS FAILING (") and line.endswith(")"):
            flags.append(TicksFailing(line[len("TICKS FAILING ("):-1]))
        elif line.startswith(f"FORM WAITING on you in {session}: "):
            flags.append(FormWaiting(line.partition(": ")[2]))
        elif line.startswith(f"STILL WAITING on you, {session}:"):
            flags.append(StillWaiting(line.partition(":")[2].strip()))
        elif (match := re.fullmatch(rf"BLOCK in {re.escape(session)}, open ([^:]+):\s*(.*)", line)):
            flags.append(Block(match.group(1), match.group(2)))
        else:
            activity.append(Activity(line))
    if session:
        if decision is not None:
            raise DailiesFailure("status", f"{session}: unclosed DECISION block")
        blocks.append(StatusBlock(session, state, tuple(flags), tuple(activity)))
    missing = sorted(names - {block.session for block in blocks})
    if missing:
        raise DailiesFailure("status", f"missing unit blocks: {', '.join(missing)}")
    return tuple(blocks)


# The time an ETA line states: the clock, `+N` days, and the zone word after it when there is one.
ETA_TIME = re.compile(r"\b(\d{1,2}:\d{2})(\+\d+)?(?: ([A-Z]{2,4})\b)?")
# Zone words a unit writes after an ETA. An unknown word leaves the time as written.
ETA_ZONES: dict[str, tzinfo] = {
    **{word: timezone(timedelta(hours=hours)) for word, hours in (
        ("UTC", 0), ("GMT", 0), ("EDT", -4), ("EST", -5), ("CDT", -5), ("CST", -6),
        ("MDT", -6), ("MST", -7), ("PDT", -7), ("PST", -8))},
    **{word: ZoneInfo(name) for word, name in (
        ("ET", "America/New_York"), ("CT", "America/Chicago"),
        ("MT", "America/Denver"), ("PT", "America/Los_Angeles"))},
}


def in_report_zone(line: str, now: datetime, zone: ZoneInfo) -> str:
    """`line` with a time stated in another zone rewritten as a clock in the report's zone."""
    found = ETA_TIME.search(line)
    stated = ETA_ZONES.get(found.group(3) or "") if found is not None else None
    if found is None or stated is None:
        return line
    hour, minute = found.group(1).split(":")
    days = int(found.group(2) or 0)
    there = now.replace(tzinfo=zone).astimezone(stated)
    local = (there.replace(hour=int(hour), minute=int(minute)) + timedelta(days=days)).astimezone(zone)
    # A line that named no day keeps none: the renderer's own rule decides today or tomorrow.
    ahead = (local.date() - now.date()).days if days else 0
    clock = f"{local:%H:%M}" + (f"+{ahead}" if ahead > 0 else "")
    return line[:found.start()] + clock + line[found.end():]


def eta_state(block: StatusBlock, phase: str, seen: JsonMap, now: datetime, zone: ZoneInfo, *, held: bool) -> EtaState:
    lines = list(dict.fromkeys(item.text for item in block.activity if "ETA" in item.text))
    if not lines:
        return EtaNone()
    key = f"{block.session}|{phase}"
    prior = seen.get(key)
    requested = isinstance(eta_requested(seen, key), EtaRequested)
    record = cast(JsonMap, prior) if isinstance(prior, dict) else {}
    also = record.get("also")
    known = {**(cast(JsonMap, also) if isinstance(also, dict) else {}), record.get("text"): record.get("first_seen")}
    first = {text: datetime.fromisoformat(stamp) if isinstance(stamp := known.get(text), str) else now for text in lines}
    # The pane's lowest ETA line is not its newest: a pinned table sits below later messages. The
    # line first seen most recently is the one the unit stated last; among equals the lowest wins.
    line = max(reversed(lines), key=lambda text: first[text])
    first_seen = first[line]
    unchanged = record.get("text") == line and isinstance(record.get("first_seen"), str)
    others = {text: first[text].isoformat(timespec="minutes") for text in lines if text != line}
    seen[key] = {"text": line, "first_seen": first_seen.isoformat(timespec="minutes"), "requested": requested,
                 **({"also": others} if others else {})}
    shown = in_report_zone(line, now, zone)
    # A held unit cannot move, so the ETA it had is kept and no new one is asked for. One that had gone
    # old or passed before the hold, so that a new one was asked for, is not brought back as current.
    if held and unchanged and not requested:
        return EtaFresh(shown)
    time = re.search(r"\b(\d{1,2}:\d{2}(?:\+\d+)?)\b", shown)
    if time is not None:
        if parse_time(time.group(1), now) < now:
            return EtaPassed()
    if now - first_seen > timedelta(hours=1):
        return EtaStale(shown, first_seen)
    return EtaFresh(shown)


def eta_value(state: EtaState, supplied: object, requested: bool = False) -> JsonMap:
    fields = as_map(supplied, "judgment.eta") if supplied is not None else {}
    result = dict(fields)
    if isinstance(state, EtaPassed):
        return {"none": "none measured - requested"}
    if isinstance(state, EtaNone):
        if requested:
            return {"none": "none measured - requested"}
        none = fields.get("none")
        return {"none": none if isinstance(none, str) else "no ETA stated yet"}
    found = re.search(r"\b(\d{1,2}:\d{2}(?:\+\d+)?)\b", state.text)
    if found:
        result["time"] = found.group(1)
        _ = result.pop("none", None)
    if isinstance(state, EtaStale):
        result["detail"] = f"set {state.first_seen.strftime('%H:%M')}"
    return result


def command(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [str(path), *args] if os.access(path, os.X_OK) else [sys.executable, str(path), *args]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def next_due(output: str, now: datetime, zone: ZoneInfo) -> str:
    match = re.search(r"(?m)^next_due=(\d+) \([^)]+\)", output)
    if match is None:
        raise DailiesFailure("clock", f"next_due missing: {output.strip()}")
    due = datetime.fromtimestamp(int(match.group(1)), zone)
    days = (due.date() - now.date()).days
    return due.strftime("%H:%M") + (f"+{days}" if days > 0 else "")


def validate_report(result: JsonMap, holders_path: Path, judgment_path: Path) -> None:
    previous_holders = os.environ.get("BUILD_HOLD_DIR")
    os.environ["BUILD_HOLD_DIR"] = str(holders_path)
    try:
        try:
            _ = parse_report(result, "default")
        except InputError as error:
            raise DailiesFailure("judgment", f"{judgment_path}: {error}") from error
    finally:
        if previous_holders is None:
            _ = os.environ.pop("BUILD_HOLD_DIR", None)
        else:
            os.environ["BUILD_HOLD_DIR"] = previous_holders


def run(args: argparse.Namespace) -> int:
    production_path = cast(Path, args.production)
    status_path = cast(Path, args.status)
    judgment_path = cast(Path, args.judgment)
    state_dir = cast(Path, args.state_dir)
    out = cast(Path, args.out)
    holders_arg = cast(Path | None, args.holders)
    notifier_arg = cast(Path | None, args.notifier)
    length = cast(str, args.length)
    user_run = cast(bool, args.user_run)
    at = cast(str | None, args.at)
    render_state = cast(Path, args.render_state)
    production = read_production(production_path)
    now, _ = local_now(str(production.zone), "--zone", at)
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    units = units_from_doc(lines, production.slug)
    instance = f"showrunner-{production.slug}"
    blocks = status_blocks(status_path, units)
    report("status", "ok", f"{len(blocks)} units read")
    for block in blocks:
        first: list[str] = []
        if isinstance(block.state, SessionGone):
            first.append("SESSION GONE")
        elif isinstance(block.state, ClaudeNotRunning):
            first.append("CLAUDE NOT RUNNING")
        for flag in block.flags:
            if isinstance(flag, FormWaiting):
                first.append("FORM WAITING")
            elif isinstance(flag, Decision):
                first.append("DECISION")
        if any("usage limit" in activity.text.lower() for activity in block.activity):
            first.append("Usage limit")
        if first:
            print(f"flags first: {block.session}: {', '.join(first)}")
    raw = cast(object, json.loads(judgment_path.read_text(encoding="utf-8")))
    judgment = as_map(raw, str(judgment_path))
    supplied_units = as_list(judgment.get("units"), f"{judgment_path}.units")
    by_session: dict[str, JsonMap] = {}
    for index, supplied in enumerate(supplied_units):
        fields = as_map(supplied, f"{judgment_path}.units[{index}]")
        unit_name = fields.get("unit")
        if not isinstance(unit_name, str):
            raise DailiesFailure("judgment", f"{judgment_path}.units[{index}].unit: required")
        by_session[unit_name] = fields
    seen_path = state_dir / "eta_seen.json"
    seen = as_map(cast(object, json.loads(seen_path.read_text())) if seen_path.exists() else {}, str(seen_path))
    holders_path = holders_arg or Path.home() / ".local/state/build-hold"
    holders = read_holders(holders_path)
    holder_names: set[str] = {holder.name for holder in holders.holders} if isinstance(holders, ActiveHolders) else set()
    units_out: list[JsonMap] = []
    eta_touched: dict[str, None] = {}
    eta_request_candidates: dict[str, str] = {}
    missing: list[str] = []
    blocks_by_session = {block.session: block for block in blocks}
    for index, unit in enumerate(units):
        block = blocks_by_session[unit.name]
        fields = by_session.get(unit.name, {})
        result = dict(fields)
        result["unit"] = unit.name
        if "build_hold" in result:
            raise DailiesFailure("build hold", f"{judgment_path}.units[{index}].build_hold: supplied by holder files")
        if holder_names and unit.session not in holder_names and unit.name not in holder_names:
            result["build_hold"] = True
        if "needs_user" not in result and any(isinstance(flag, (FormWaiting, Decision, StillWaiting)) for flag in block.flags):
            result["needs_user"] = True
        phase = fields.get("phase")
        phase_text = phase if isinstance(phase, str) else ""
        key = f"{unit.name}|{phase_text}"
        held = fields.get("held")
        state = eta_state(block, phase_text, seen, now, ZoneInfo(str(production.zone)),
                          held=isinstance(held, str) and bool(held.strip()))
        if not isinstance(state, EtaNone):
            eta_touched[key] = None
        requested = isinstance(eta_requested(seen, key), EtaRequested)
        result["eta"] = eta_value(state, fields.get("eta"), requested)
        if isinstance(state, (EtaStale, EtaPassed)):
            record = seen.get(key)
            if isinstance(record, dict) and isinstance(eta_requested(seen, key), EtaNotYetRequested):
                eta_request_candidates[key] = unit.name
                cast(JsonMap, record)["requested"] = True
        for key in ("project", "phase", "started", "held", "update"):
            if key not in result or (key != "held" and result[key] is None):
                missing.append(f"units[{index}].{key}: required")
        units_out.append(result)
    if missing:
        raise DailiesFailure("judgment", f"{judgment_path}: " + "; ".join(missing))
    if bool(holder_names) != any(bool(unit.get("build_hold")) for unit in units_out):
        raise DailiesFailure("build hold", "units.build_hold: holder files and unit markers disagree")
    report("build hold", "ok", f"{len(holder_names)} active holders checked")
    try:
        review = review_watch(production, state_dir)
    except PointFailure as error:
        raise DailiesFailure("review watch", error.detail) from error
    topics = as_list(judgment.get("topics"), f"{judgment_path}.topics")
    topics_out = [as_map(topic, f"{judgment_path}.topics[{index}]") for index, topic in enumerate(topics)]
    review_line = review.line
    if review_line and not review_line.startswith("acknowledged"):
        topics_out.append({"title": "Review watch", "update": review_line, "eta": "no ETA stated yet",
                           "needs_user": isinstance(review, (WatchFirstAlert, WatchRepeat))})
    report("review watch", "ok", review_line or "no open review watch")
    if isinstance(review, WatchFirstAlert):
        print(review.table)
    history = merge_branch_history(production.checkout, production.merge_branch)
    last = history.last_merge()
    local = git(production.checkout, "rev-parse", production.merge_branch)
    remote = git(production.checkout, "rev-parse", f"origin/{production.merge_branch}")
    if local.returncode:
        raise DailiesFailure("merge branch", local.stderr.strip())
    pushed = remote.returncode == 0 and local.stdout.strip() == remote.stdout.strip()
    merge = as_map(judgment.get("merge", {}), f"{judgment_path}.merge")
    held, testing = merge.get("held"), merge.get("testing")
    if held or testing or not pushed:
        latest = "no checkpoint merged" if isinstance(last, NoMerge) else f"{last.unit} phase {last.phase} ({last.short})"
        details = [f"last merge: {latest}", "pushed" if pushed else "not pushed"]
        if held:
            details.append(f"held: {held}")
        if testing:
            details.append(f"testing: {testing}")
        topics_out.append({"title": "Merge branch", "update": "; ".join(details),
                           "eta": str(merge.get("eta") or "no ETA stated yet"), "needs_user": bool(merge.get("needs_user", False))})
    report("merge branch", "ok", "pushed" if pushed else "not pushed")
    result: JsonMap = {"length": length, "zone": str(production.zone), "units": units_out, "topics": topics_out}
    validate_report(result, holders_path, judgment_path)
    checked = check_render_state(result, render_state, at)
    if isinstance(checked, StateRefused):
        raise DailiesFailure("judgment", f"{checked.field}: {checked.why}")
    notifier = notifier_arg or Path(__file__).resolve().parent.parent / "message/notifier.sh"
    if user_run:
        production.log.parent.mkdir(parents=True, exist_ok=True)
        with production.log.open("a", encoding="utf-8"):
            pass
    action = "restart" if user_run else "status"
    clock = command(notifier, action, instance)
    if clock.returncode:
        raise DailiesFailure("clock", clock.stderr.strip() or clock.stdout.strip())
    result["next_run"] = next_due(clock.stdout, now, production.zone)
    if user_run:
        production.log.parent.mkdir(parents=True, exist_ok=True)
        with production.log.open("a", encoding="utf-8") as log:
            _ = log.write(next(line for line in clock.stdout.splitlines() if line.startswith("next_due=")) + "\n")
    report("clock", "ok", str(result["next_run"]))
    validate_report(result, holders_path, judgment_path)
    report("judgment", "ok", f"{len(units_out)} units accepted")
    def merge_eta_records(fresh: JsonMap) -> tuple[str, ...]:
        requests: list[str] = []
        for key in eta_touched:
            computed = seen.get(key)
            if not isinstance(computed, dict):
                continue
            fresh_state = eta_requested(fresh, key)
            fields = cast(JsonMap, computed).copy()
            fields["requested"] = (isinstance(fresh_state, EtaRequested)
                                   or isinstance(eta_requested(seen, key), EtaRequested))
            fresh[key] = fields
            if key in eta_request_candidates and isinstance(fresh_state, EtaNotYetRequested):
                requests.append(eta_request_candidates[key])
        return tuple(requests)

    eta_requests = update_state(seen_path, merge_eta_records)
    for session in eta_requests:
        print(f"request /unit:eta: {session}")
    out.parent.mkdir(parents=True, exist_ok=True)
    _ = out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    report("input", "ok", f"wrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("production", "status", "judgment", "state-dir", "out"):
        _ = parser.add_argument(f"--{name}", required=True, type=Path)
    _ = parser.add_argument("--length", choices=("simple", "page", "elaborate"), default="simple")
    _ = parser.add_argument("--holders", type=Path)
    _ = parser.add_argument("--notifier", type=Path)
    _ = parser.add_argument("--render-state", required=True, type=Path)
    _ = parser.add_argument("--at")
    _ = parser.add_argument("--user-run", action="store_true")
    args = parser.parse_args()
    try:
        return run(args)
    except (DailiesFailure, InputError, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        step = error.step if isinstance(error, DailiesFailure) else "input"
        report(step, "failed", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
