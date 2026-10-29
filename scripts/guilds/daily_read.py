#!/usr/bin/env python3
"""Print a plain guild summary or full JSON for one UTC interval; read no turns or live sessions."""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.direct import phase_process, progress_history  # noqa: E402
from scripts.guilds import astra_work, tips  # noqa: E402
from scripts.history import rounds  # noqa: E402
from scripts.lib import unit_runs  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402


@dataclass(frozen=True)
class LocalSources:
    run_roots: tuple[Path, ...]
    history_root: Path
    astra_root: Path
    roles_root: Path
    host: str

    @classmethod
    def configured(cls) -> LocalSources:
        roots = os.environ.get("GUILD_DAILY_RUN_ROOTS")
        return cls(tuple(Path(part).expanduser() for part in roots.split(os.pathsep) if part)
                   if roots is not None else unit_runs.run_roots(),
                   Path(os.environ.get("GUILD_DAILY_HISTORY_ROOT") or str(unit_runs.history_root() / "runs")).expanduser(),
                   Path(os.environ.get("GUILD_DAILY_ASTRA_ROOT") or
                        os.environ.get("ASTRA_REVIEW_DIR") or "/tmp/claude/astra").expanduser(),
                   Path(os.environ.get("GUILD_DAILY_ROLES_ROOT") or
                        os.environ.get("GUILD_ROLES_STATE") or "~/.local/state/guilds/roles").expanduser(),
                   os.environ.get("GUILD_DAILY_HOST") or socket.gethostname())


@dataclass(frozen=True)
class SourcedEvent:
    fields: dict[str, object]
    source: str


@dataclass(frozen=True)
class SavedAssignment:
    assignment: astra_work.AstraWork
    source: str


@dataclass(frozen=True)
class Problem:
    state: str
    source: str
    reason: str


def text(record: dict[str, object], name: str) -> str:
    value = record.get(name)
    return value if isinstance(value, str) else ""


def number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else 0.0


def utc_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.utcoffset() != timedelta(0):
        raise ValueError("time must have a UTC offset: " + value)
    return moment.astimezone(UTC)


def epoch(record: dict[str, object]) -> float:
    if "timestamp_epoch" in record:
        return number(record["timestamp_epoch"])
    for key in ("timestamp", "timestamp_utc", "assigned_at_utc"):
        if text(record, key):
            try:
                return utc_time(text(record, key)).timestamp()
            except ValueError:
                return 0.0
    return 0.0


def object_file(path: Path, problems: list[Problem]) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return cast(dict[str, object], value)
    except (OSError, UnicodeError, ValueError) as error:
        problems.append(Problem("EvidenceUnavailable", str(path), str(error)))
        return {}


def event_files(paths: set[Path], problems: list[Problem]) -> list[SourcedEvent]:
    result: list[SourcedEvent] = []
    seen: set[str] = set()
    for path in sorted(paths):
        try:
            with path.open(encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    source = f"{path}:{line_number}"
                    try:
                        value = cast(object, json.loads(line))
                        if not isinstance(value, dict):
                            raise ValueError("expected an object")
                        fields = cast(dict[str, object], value)
                        identifier = text(fields, "event_id")
                        if identifier and identifier in seen:
                            continue
                        if identifier:
                            seen.add(identifier)
                        result.append(SourcedEvent(fields, source))
                    except ValueError as error:
                        problems.append(Problem("EvidenceInvalid", source, str(error)))
        except (OSError, UnicodeError) as error:
            problems.append(Problem("EvidenceUnavailable", str(path), str(error)))
    return sorted(result, key=lambda event: epoch(event.fields))


def discover(sources: LocalSources, problems: list[Problem]) -> tuple[dict[str, Path], set[Path]]:
    runs: dict[str, Path] = {}
    histories = set(sources.history_root.glob("*.jsonl"))
    for root in sources.run_roots:
        if not root.exists():
            problems.append(Problem("HistoryNotRecorded", str(root), "run root is absent"))
            continue
        try:
            directories = sorted(root.iterdir())
        except OSError as error:
            problems.append(Problem("EvidenceUnavailable", str(root), str(error)))
            continue
        for directory in directories:
            if not directory.is_dir() or directory.name == "active":
                continue
            state_path = directory / "progress_history_state.json"
            state = object_file(state_path, problems) if state_path.is_file() else {}
            run_id = text(state, "run_id") or directory.name
            if run_id in runs and runs[run_id].resolve() != directory.resolve():
                problems.append(Problem("RunAmbiguous", str(directory), f"duplicate run {run_id}"))
            else:
                runs[run_id] = directory
            history = text(state, "history_file")
            if history:
                histories.add(Path(history).expanduser())
    if not histories:
        problems.append(Problem("HistoryNotRecorded", str(sources.history_root), "no run ledgers"))
    return runs, histories


def saved_assignments(sources: LocalSources, runs: dict[str, Path], guild: str,
                      problems: list[Problem], since: float = 0) -> list[SavedAssignment]:
    if guild not in ("designers", "reviewers"):
        return []
    paths = {path for run in runs.values() for path in run.glob("design/phase*/round*_brief.md")}
    paths.update(sources.astra_root.glob("*/prompt.md"))
    paths.update(sources.roles_root.glob("*/prompts/*.md"))
    role = "astra-designer" if guild == "designers" else "astra-reviewer"
    assignments: list[SavedAssignment] = []
    for path in sorted(paths):
        header = astra_work.read_header(path)
        if isinstance(header, astra_work.HeaderMissing):
            if path.parent.parent == sources.astra_root:
                try:
                    if path.stat().st_mtime < since:
                        continue
                except OSError as error:
                    problems.append(Problem("EvidenceUnavailable", str(path), str(error)))
                    continue
            problems.append(Problem("MembershipNotRecorded", header.path, "assignment header is absent"))
        elif isinstance(header, astra_work.HeaderInvalid):
            problems.append(Problem("HeaderInvalid", header.path, header.reason))
        elif header.role == role:
            if not Path(header.document).is_absolute() or (isinstance(header.work, astra_work.PhaseWork)
                                                          and not Path(header.work.run_directory).is_absolute()):
                problems.append(Problem("HeaderInvalid", str(path), "document and run directory must be absolute"))
                continue
            assignments.append(SavedAssignment(header, str(path)))
    if not paths:
        problems.append(Problem("MembershipNotRecorded", str(sources.roles_root), "no saved assignment headers"))
    return assignments


def join_events(events: list[SourcedEvent], problems: list[Problem]) -> list[SourcedEvent]:
    owners: dict[str, set[str]] = defaultdict(set)
    for event in events:
        fields = event.fields
        if text(fields, "event_type") == "phase_started" and text(fields, "run_id"):
            owners[text(fields, "phase_instance_id")].add(text(fields, "run_id"))
    joined: list[SourcedEvent] = []
    for event in events:
        fields = event.fields
        instance = text(fields, "phase_instance_id")
        if instance and not text(fields, "run_id"):
            if len(owners[instance]) == 1:
                fields = {**fields, "run_id": next(iter(owners[instance]))}
            else:
                problems.append(Problem("PhaseUnassigned", event.source,
                                        f"ambiguous phase instance {instance}: {len(owners[instance])} possible runs"))
        joined.append(SourcedEvent(fields, event.source))
    return joined


def ledger_titles(runs: dict[str, Path], problems: list[Problem]) -> dict[tuple[str, str, str], tuple[str, str]]:
    titles: dict[tuple[str, str, str], tuple[str, str]] = {}
    for run_id, directory in runs.items():
        for path in sorted(directory.glob("findings_state*.json")):
            state = object_file(path, problems)
            instance = text(state, "phase_instance_id")
            findings = state.get("findings")
            if not isinstance(findings, dict):
                problems.append(Problem("FindingsNotRecorded", str(path), "findings object is absent"))
                continue
            for identifier, value in cast(dict[str, object], findings).items():
                if isinstance(value, dict):
                    finding = cast(dict[str, object], value)
                    title = text(finding, "title")
                    if title and instance:
                        titles[(run_id, instance, identifier)] = (title, str(path))
    return titles


def repeated_titles(events: list[SourcedEvent], titles: dict[tuple[str, str, str], tuple[str, str]],
                    window: phase_process.Window) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], dict[tuple[str, int], SourcedEvent]] = defaultdict(dict)
    landed: dict[tuple[str, str, str, int], SourcedEvent] = {}
    verdicts: set[tuple[str, str, str, int]] = set()
    result: list[dict[str, object]] = []

    def row(state: str, title: str, current: SourcedEvent, prior: SourcedEvent | str) -> dict[str, object]:
        evidence = [prior, current] if isinstance(prior, SourcedEvent) else [current]
        return {"state": state, "run_id": text(current.fields, "run_id"), "title": title,
                "event_ids": [text(item.fields, "event_id") for item in evidence if text(item.fields, "event_id")],
                "sources": [item.source for item in evidence] + ([prior] if isinstance(prior, str) and prior else []),
                "phase_refs": list(dict.fromkeys(text(item.fields, "phase_instance_id") for item in evidence)),
                "timestamp_epoch": epoch(current.fields)}

    for event in events:
        fields = event.fields
        kind = text(fields, "event_type")
        run, instance, identifier = (text(fields, key) for key in ("run_id", "phase_instance_id", "finding_id"))
        key = (run, instance, identifier)
        round_value = fields.get("round")
        valid_round = type(round_value) is int and round_value >= 0
        round_number = cast(int, round_value) if valid_round else -1
        if kind == "finding_opened":
            title = text(fields, "title")
            if not title:
                continue
            titles[key] = (title, event.source)
            if not (run and instance and valid_round and text(fields, "event_id")):
                if window.contains(epoch(fields)):
                    result.append(row("RepeatNotProven", title, event, ""))
                continue
            normalized = " ".join(title.casefold().split())
            group = groups[(run, normalized)]
            identity = (instance, round_number)
            if identity not in group:
                if group and window.contains(epoch(fields)):
                    result.append(row("RepeatedTitle", title, event, next(iter(group.values()))))
                group[identity] = event
        elif kind == "finding_batch_landed" and run and instance and valid_round:
            identifiers = fields.get("landed", [])
            if isinstance(identifiers, list):
                for item in cast(list[object], identifiers):
                    if isinstance(item, str):
                        landed[(run, instance, item, round_number)] = event
        elif kind == "finding_verdict" and text(fields, "verdict") in ("still_open", "reopened"):
            verdict_key = (*key, round_number)
            if verdict_key in verdicts:
                continue
            verdicts.add(verdict_key)
            if not window.contains(epoch(fields)):
                continue
            title, source = titles.get(key, ("TitleNotRecorded", event.source))
            earlier_repairs = [repair for repair_key, repair in landed.items()
                               if repair_key[:3] == key and repair_key[3] <= round_number
                               and epoch(repair.fields) < epoch(fields)]
            repair = max(earlier_repairs, key=lambda repair: epoch(repair.fields), default=None)
            proven = (run and instance and valid_round and repair is not None
                      and epoch(repair.fields) < epoch(fields) and text(repair.fields, "event_id")
                      and text(fields, "event_id") and title != "TitleNotRecorded")
            repeated = row("RepeatedTitle" if proven else "RepeatNotProven", title, event,
                           repair if repair is not None else source)
            if source not in cast(list[str], repeated["sources"]):
                cast(list[str], repeated["sources"]).append(source)
            result.append(repeated)
    return result


def phase_rows(events: list[SourcedEvent], measured: tuple[phase_process.Event, ...],
               assignments: list[SavedAssignment], guild: str, window: phase_process.Window) -> list[dict[str, object]]:
    all_phases = rounds.load_phases([event.fields for event in events])
    selected = {(text(event.fields, "run_id"), text(event.fields, "phase_instance_id"))
                for event in events if window.contains(epoch(event.fields))}
    members: dict[tuple[str, str], set[str]] = defaultdict(set)
    if guild in ("designers", "reviewers"):
        for saved in assignments:
            work = saved.assignment.work
            if isinstance(work, astra_work.PhaseWork):
                key = (work.run_id, work.phase_instance_id)
                if key in selected or window.contains(utc_time(saved.assignment.assigned_at_utc).timestamp()):
                    members[key].add(saved.assignment.member)
        selected.intersection_update(members)
    else:
        director_runs = {text(event.fields, "run_id") for event in events
                         if text(event.fields, "skill") in (progress_history.SKILL_NAME, progress_history.FORMER_SKILL_NAME)}
        selected = {key for key in selected if guild == "units" and key[0] in director_runs}
        directors = {text(event.fields, "run_id"): text(cast(dict[str, object], event.fields["main_agent"]), "session_id")
                     for event in events if text(event.fields, "event_type") == "run_started"
                     and isinstance(event.fields.get("main_agent"), dict)}
        for key in selected:
            members[key].add(directors.get(key[0]) or "MembershipNotRecorded")

    baseline_end = datetime.fromtimestamp(window.since, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    baseline = phase_process.Window((baseline_end - timedelta(days=14)).timestamp(), baseline_end.timestamp())
    baseline_bounds = phase_process.phase_boundaries(measured, baseline)
    baseline_values: dict[str, list[float]] = defaultdict(list)
    for totals in phase_process.allocated_step_seconds(measured, baseline_bounds):
        for step, seconds in totals.items():
            baseline_values[step].append(seconds)
    bounds = phase_process.phase_boundaries(measured, window)
    allocations = {(bound.run, bound.instance): values for bound, values in
                   zip(bounds, phase_process.allocated_step_seconds(measured, bounds))}
    results: list[dict[str, object]] = []
    for phase in all_phases:
        key = (phase["run_id"], phase["phase_instance_id"])
        if key not in selected:
            continue
        steps: list[dict[str, object]] = []
        flags: list[str] = []
        phase_events = [event for event in events
                        if (text(event.fields, "run_id"), text(event.fields, "phase_instance_id")) == key]
        repairs = [repair for repair in rounds.phase_rounds([event.fields for event in phase_events])
                   if window.contains(repair["started_at"])]
        if any(repair["round"] >= 4 for repair in repairs):
            flags.append("RepairRoundsBeyondThree")
        if phase["check"] == "mixed":
            flags.append("MixedPhaseRecord")
        for label, seconds in allocations.get(key, {}).items():
            samples = baseline_values[label]
            threshold: float | str = phase_process.summarize(label, samples, 0).p90 if samples else "ThresholdNotRecorded"
            known = label != "director time"
            verdict = "InsufficientSamples" if len(samples) < 20 else "WithinThreshold"
            if known and len(samples) >= 20 and isinstance(threshold, float) and seconds > threshold:
                verdict = "Slow"
                flags.append("SlowStep")
            steps.append({"step": label if known else "unattributed wall", "seconds": seconds,
                          "threshold_seconds": threshold, "sample_count": len(samples),
                          "speed_verdict": verdict if known else "CoverageUnknown",
                          "coverage": "Recorded" if known else "CoverageUnknown"})
        sources = sorted({event.source for event in phase_events})
        pending: dict[tuple[str, str], str] = {}
        for event in phase_events:
            kind = text(event.fields, "event_type")
            is_pass = kind.startswith("pass_")
            identifier = text(event.fields, "pass_instance_id" if is_pass else "activity_instance_id")
            label = text(event.fields, "pass_kind" if is_pass else "activity_label")
            if kind in ("pass_started", "activity_started"):
                pending[(identifier, "pass" if is_pass else "activity")] = label
            elif kind in ("pass_finished", "activity_finished"):
                _ = pending.pop((identifier, "pass" if is_pass else "activity"), "")
        unknown_steps = [{"step": label, "state": "CoverageUnknown"} for label in pending.values()]
        results.append({"run_id": key[0], "phase_instance_id": key[1], "phase_id": phase["phase_id"],
                        "members": sorted(members[key]) or [key[0]], "repair_rounds": len(repairs),
                        "rounds": [{"round": repair["round"], "started_at": repair["started_at"],
                                    "finished_at": repair["finished_at"], "cause": repair["cause"],
                                    "passes": repair["passes"], "covered": repair["covered"]}
                                   for repair in repairs],
                        "mixed": phase["check"] == "mixed", "steps": steps, "unknown_steps": unknown_steps,
                        "step_coverage": "Recorded" if key in allocations and not pending else "CoverageUnknown",
                        "baseline": {"since": baseline.since, "until": baseline.until, "days": 14},
                        "sources": sources, "flags": flags})
    return results


def read_daily(guild: str, since: datetime, until: datetime, sources: LocalSources) -> dict[str, object]:
    if (guild not in tips.GUILDS or since.utcoffset() != timedelta(0)
            or until.utcoffset() != timedelta(0) or since >= until):
        raise ValueError("give a known guild and an increasing UTC interval")
    window = phase_process.Window(since.timestamp(), until.timestamp())
    problems: list[Problem] = []
    if guild == "showrunners":
        problems.append(Problem("HistoryNotRecorded", "WriterNotRecorded",
                                "no showrunner run writer records exist yet"))
    runs, histories = discover(sources, problems)
    assignments = [saved for saved in saved_assignments(sources, runs, guild, problems, window.since)
                   if utc_time(saved.assignment.assigned_at_utc).timestamp() < window.until]
    for saved in assignments:
        if isinstance(saved.assignment.work, astra_work.PhaseWork):
            work = saved.assignment.work
            histories.add(Path(work.history_file))
            _ = runs.setdefault(work.run_id, Path(work.run_directory))
            if not Path(work.run_directory).is_dir():
                problems.append(Problem("EvidenceUnavailable", work.run_directory, "saved run directory is absent"))
    events = join_events([event for event in event_files(histories, problems)
                          if epoch(event.fields) < window.until], problems)
    unreadable = {problem.source for problem in problems if problem.state == "EvidenceUnavailable"}
    measured, timing_problems = phase_process.read_events(path for path in histories if str(path) not in unreadable)
    measured = tuple(event for event in measured if event.at < window.until)
    problems.extend(Problem("HistoryInvalid", problem, "step timing unavailable for this record")
                    for problem in timing_problems)
    titles = ledger_titles(runs, problems)
    phases = phase_rows(events, measured, assignments, guild, window)
    phase_keys = {(text(phase, "run_id"), text(phase, "phase_instance_id")) for phase in phases}
    assignment_rows: list[dict[str, object]] = []
    for saved in assignments:
        assignment = saved.assignment
        work = assignment.work
        key = (work.run_id, work.phase_instance_id) if isinstance(work, astra_work.PhaseWork) else ("", "")
        if not window.contains(utc_time(assignment.assigned_at_utc).timestamp()) and key not in phase_keys:
            continue
        outcome = "OutputRecorded" if Path(assignment.reply_path).is_file() else "OutcomeUnknown"
        assignment_rows.append({**asdict(assignment), "source": saved.source, "outcome": outcome})
        for path in (assignment.document, assignment.reply_path):
            if not Path(path).is_file():
                problems.append(Problem("EvidenceUnavailable", path, "saved assignment evidence is absent"))
        if isinstance(work, astra_work.PhaseWork) and key not in phase_keys:
            problems.append(Problem("HistoryNotRecorded", saved.source,
                                    f"phase {work.run_id}/{work.phase_instance_id} has no interval metrics"))
    repeats = repeated_titles(events, titles, window)
    selected_runs = {text(phase, "run_id") for phase in phases}
    repeats = [row for row in repeats if text(row, "run_id") in selected_runs]
    waits: list[dict[str, object]] = [{"state": "HistoryNotRecorded", "source": "WriterNotRecorded",
                                      "reason": "no wait writer records exist yet"}]
    receipts: list[dict[str, object]] = [{"state": "ReceiptMissing", "source": "WriterNotRecorded",
                                         "reason": "no merge receipt writer records exist yet"}]
    flagged: list[dict[str, object]] = []
    for phase in phases:
        for flag in cast(list[str], phase["flags"]):
            flagged.append({"kind": flag, "run_id": phase["run_id"], "phase_instance_id": phase["phase_instance_id"],
                            "sources": phase["sources"]})
    flagged.extend({"kind": "RepeatedTitle", **row} for row in repeats if row["state"] == "RepeatedTitle")
    members = {text(row, "member") for row in assignment_rows}
    if guild in ("units", "showrunners"):
        members.update(member for phase in phases for member in cast(list[str], phase["members"]))
    members.discard("MembershipNotRecorded")
    if not members:
        roots = [*sources.run_roots, sources.history_root]
        if guild in ("designers", "reviewers"):
            roots.extend((sources.astra_root, sources.roles_root))
        problems.append(Problem("MembershipNotRecorded", "; ".join(str(root) for root in roots),
                                f"no {guild} member recorded in the interval"))
    run_ids = {text(phase, "run_id") for phase in phases}
    return {"host": sources.host, "guild": guild,
            "interval": {"since": since.isoformat(), "until": until.isoformat(),
                         "local_since": since.astimezone(user_zone()).isoformat(),
                         "local_until": until.astimezone(user_zone()).isoformat(),
                         "local_zone": since.astimezone(user_zone()).tzname() or "ZoneNotRecorded"},
            "coverage": {"members": len(members), "runs": len(run_ids), "assignments": len(assignment_rows),
                         "phases": len(phases), "partial": bool(problems)},
            "counts": {"phases": len(phases), "assignments": len(assignment_rows),
                       "repair_rounds": sum(cast(int, phase["repair_rounds"]) for phase in phases),
                       "repeated_titles": sum(row["state"] == "RepeatedTitle" for row in repeats),
                       "flagged": len(flagged), "problems": len(problems)},
            "phases": phases, "assignments": assignment_rows, "repeats": repeats,
            "waits": waits, "receipts": receipts, "problems": [asdict(problem) for problem in problems],
            "flagged": flagged}


def render(report: dict[str, object]) -> str:
    interval = cast(dict[str, str], report["interval"])
    coverage = cast(dict[str, object], report["coverage"])
    counts = cast(dict[str, int], report["counts"])
    lines = [f"Host: {report['host']} | Guild: {report['guild']}",
             f"UTC: {interval['since']} to {interval['until']}",
             f"Local ({interval['local_zone']}): {interval['local_since']} to {interval['local_until']}",
             "Coverage: " + ", ".join(f"{name} {coverage[name]}" for name in
                                     ("members", "runs", "assignments", "phases"))
             + f"; partial {'yes' if coverage['partial'] else 'no'}",
             "Counts: " + ", ".join(f"{name.replace('_', ' ')} {value}" for name, value in counts.items())]
    for phase in cast(list[dict[str, object]], report["phases"]):
        members = ", ".join(cast(list[str], phase["members"]))
        flags = ", ".join(cast(list[str], phase["flags"])) or "no flags"
        lines.append(f"Phase {phase['phase_id']} | run {phase['run_id']} | members {members} | "
                     + f"repair rounds started in window {phase['repair_rounds']} | {flags}")
    for repeat in cast(list[dict[str, object]], report["repeats"]):
        phases = ", ".join(cast(list[str], repeat["phase_refs"]))
        lines.append(f"Repeat: {repeat['state']} | run {repeat['run_id']} | phases {phases} | {repeat['title']}")
    for flagged in cast(list[dict[str, object]], report["flagged"]):
        detail = text(flagged, "title") or text(flagged, "phase_instance_id")
        lines.append(f"Flag: {flagged['kind']} | run {flagged['run_id']} | {detail}")
    problems = cast(list[dict[str, str]], report["problems"])
    for state, count in sorted(Counter(problem["state"] for problem in problems).items()):
        lines.append(f"Problems: {state}: {count}")
    for problem in problems[:5]:
        lines.append(f"problem: {problem['state']}: {problem['source']}: {problem['reason']}")
    if len(problems) > 5:
        lines.append(f"and {len(problems) - 5} more")
    lines.append("Phase totals are shared context, not an individual's delay. Read turns only for flagged records.")
    return "\n".join(lines)


@dataclass
class DailyArguments(argparse.Namespace):
    guild: str = ""
    since: str = ""
    until: str = ""
    json: bool = False


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--guild", choices=tips.GUILDS, required=True)
    _ = parser.add_argument("--since", required=True)
    _ = parser.add_argument("--until", required=True)
    _ = parser.add_argument("--json", action="store_true")
    args = parser.parse_args(arguments, namespace=DailyArguments())
    try:
        report = read_daily(args.guild, utc_time(args.since), utc_time(args.until), LocalSources.configured())
    except (OSError, ValueError) as error:
        print(f"daily_read: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
