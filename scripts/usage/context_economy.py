#!/usr/bin/env python3
"""Report context measurements and save named results without changing the inputs."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import socket
import statistics
import sys
import tempfile
from typing import Literal, cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.lib import conf
from scripts.lib.user_zone import user_zone
from scripts.usage import collect, weights
from scripts.usage.agents_log import state_dir
from scripts.usage.context_economy_read import (
    CompactionEvent, Request, Session, SettingsSet, Start,
    SettingsUnknown, configured_checkout, label, read_claude, role_of,
    settings_for, settings_timeline,
)


QUANTITIES = (
    "fresh_start", "after_compaction", "at_compaction", "context_per_request",
    "cost_per_request", "requests_per_cycle", "compactions_per_active_hour", "precomputed_share",
)


@dataclass(frozen=True)
class QuantitySummary:
    """The sample count distinguishes an empty quantity from a measured zero."""

    n: int
    median: float
    mean: float
    p25: float
    p75: float


@dataclass(frozen=True)
class GroupResult:
    settings_label: str
    settings_values: dict[str, object]
    settings_commit: str
    week: str
    role: str
    model: str
    requests: int
    cli_versions: tuple[str, ...]
    quantities: dict[str, QuantitySummary]


@dataclass(frozen=True)
class Report:
    since: datetime
    until: datetime
    groups: list[GroupResult]
    left_out: int
    role_quantities: dict[str, dict[str, QuantitySummary]] = field(default_factory=dict)


def summarize(values: Sequence[float]) -> QuantitySummary:
    """Use linear interpolation at (n - 1) times the percentile."""
    if not values:
        return QuantitySummary(0, 0, 0, 0, 0)
    ordered = sorted(values)
    quartiles = statistics.quantiles(ordered, n=4, method="inclusive") if len(ordered) > 1 else ordered * 3
    return QuantitySummary(len(ordered), float(statistics.median(ordered)),
                           statistics.mean(ordered), quartiles[0], quartiles[2])


def _cost(request: Request) -> float:
    return weights.weighted({
        "model": request.model, "input": request.input, "cache_read": request.cache_read,
        "cache_write_5m": request.cache_write_5m, "cache_write_1h": request.cache_write_1h,
        "output": request.output,
    }, request.at)


def _week(at: datetime) -> str:
    year, week, _ = at.astimezone(user_zone()).isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True)
class _Measurement:
    at: datetime
    model: str
    quantity: str
    value: float


def _event_model(event: CompactionEvent, requests: Sequence[Request]) -> str:
    preceding = [request for request in requests if request.at <= event.at]
    return (preceding[-1] if preceding else requests[0]).model


def _settings_at(session: Session, at: datetime, timeline: Sequence[SettingsSet]) -> SettingsSet | SettingsUnknown:
    starts = [start for start in session.starts if start.at <= at]
    anchor = max(starts, key=lambda start: start.at).at if starts else session.first_record_at
    return settings_for(anchor, timeline) if isinstance(anchor, datetime) else SettingsUnknown()


def _measurements(session: Session, active_gap_seconds: int, *, starts: Sequence[Start],
                  timeline: Sequence[SettingsSet] = (), weekly: bool = False) -> list[_Measurement]:
    requests = sorted(session.requests, key=lambda request: request.at)
    if not requests:
        return []
    compactions = sorted(session.compactions, key=lambda event: event.at)
    result: list[_Measurement] = []
    for request in requests:
        result.extend((_Measurement(request.at, request.model, "context_per_request", float(request.context)),
                       _Measurement(request.at, request.model, "cost_per_request", _cost(request))))
    starts = sorted(starts, key=lambda start: start.at)
    for index, start in enumerate(starts):
        following = [request for request in requests if request.at >= start.at
                     and (index + 1 == len(starts) or request.at < starts[index + 1].at)]
        if start.source == "startup" and following:
            first = following[0]
            result.append(_Measurement(first.at, first.model, "fresh_start", float(first.context)))
    for event in compactions:
        model = _event_model(event, requests)
        if event.pre_tokens is not None:
            result.append(_Measurement(event.at, model, "at_compaction", float(event.pre_tokens)))
        following = [request for request in requests if request.at >= event.at]
        if following:
            first = following[0]
            result.append(_Measurement(first.at, first.model, "after_compaction", float(first.context)))
    for before, after in zip(compactions, compactions[1:]):
        count = sum(before.at <= request.at < after.at for request in requests)
        result.append(_Measurement(after.at, _event_model(after, requests), "requests_per_cycle", float(count)))

    active: dict[tuple[str, str, str], float] = defaultdict(float)
    counts: dict[tuple[str, str, str], list[CompactionEvent]] = defaultdict(list)
    moments: dict[tuple[str, str, str], datetime] = {}
    for request in requests:
        key = _rate_key(session, request.at, request.model, timeline, weekly)
        moments[key] = request.at
    for before, after in zip(requests, requests[1:]):
        gap = (after.at - before.at).total_seconds()
        if 0 < gap < active_gap_seconds:
            active[_rate_key(session, after.at, after.model, timeline, weekly)] += gap
    for event in compactions:
        key = _rate_key(session, event.at, _event_model(event, requests), timeline, weekly)
        counts[key].append(event)
        moments[key] = event.at
    for key, at in moments.items():
        events = counts[key]
        if active[key] > 0:
            result.append(_Measurement(at, key[2], "compactions_per_active_hour", len(events) * 3600 / active[key]))
        known = [event for event in events if event.kind != "unknown"]
        if known:
            share = sum(event.kind == "precomputed" for event in known) / len(known)
            result.append(_Measurement(at, key[2], "precomputed_share", share))
    return result


def _rate_key(session: Session, at: datetime, model: str, timeline: Sequence[SettingsSet],
              weekly: bool) -> tuple[str, str, str]:
    settings = _settings_at(session, at, timeline)
    commit = settings.commit if isinstance(settings, SettingsSet) else "unknown"
    return commit, _week(at) if weekly else "", model


def quantity_samples(session: Session, active_gap_seconds: int) -> dict[str, list[float]]:
    """Return measured samples; unknown compaction kinds do not enter the share."""
    result: dict[str, list[float]] = {name: [] for name in QUANTITIES}
    for measurement in _measurements(session, active_gap_seconds, starts=session.starts):
        result[measurement.quantity].append(measurement.value)
    return result


def _settings_name(settings: SettingsSet | SettingsUnknown, timeline: Sequence[SettingsSet]) -> str:
    if isinstance(settings, SettingsUnknown):
        return "unknown"
    position = next(index for index, entry in enumerate(timeline) if entry is settings)
    previous = timeline[position - 1] if position else SettingsUnknown()
    return label(settings, previous) or settings.commit


@dataclass
class _GroupSamples:
    labels: list[str] = field(default_factory=list)
    values: dict[str, object] = field(default_factory=dict)
    commits: list[str] = field(default_factory=list)
    requests: int = 0
    versions: set[str] = field(default_factory=set)
    quantities: dict[str, list[float]] = field(default_factory=lambda: {name: [] for name in QUANTITIES})


def _group_at(groups: dict[tuple[str, str, str], _GroupSamples], settings: SettingsSet | SettingsUnknown,
              timeline: Sequence[SettingsSet], at: datetime, role: str, model: str,
              by: Literal["settings", "week"]) -> _GroupSamples:
    commit = settings.commit if isinstance(settings, SettingsSet) else "unknown"
    bucket = _week(at) if by == "week" else commit
    group = groups.setdefault((bucket, role, model), _GroupSamples())
    if commit not in group.commits:
        group.labels.append(_settings_name(settings, timeline))
        group.commits.append(commit)
        if isinstance(settings, SettingsSet):
            group.values.update(settings.values)
    return group


def build_report(sessions: Sequence[Session], timeline: Sequence[SettingsSet], store: Path,
                 since: datetime, until: datetime, *, by: Literal["settings", "week"] = "settings",
                 role: str = "", settings_label: str = "") -> Report:
    """Measure whole sessions and assign each measurement the settings at its latest start."""
    configuration = conf.read("context_economy")
    minimum = configuration.whole("min_requests")
    active_gap = configuration.whole("active_gap_seconds")
    groups: dict[tuple[str, str, str], _GroupSamples] = {}
    role_samples: dict[str, dict[str, list[float]]] = {}
    left_out = 0
    for session in sessions:
        session_role = role_of(store, session.machine, session.family, session.session_id, session.agent_id)
        if role and role != session_role:
            continue
        session = replace(session,
            requests=[request for request in session.requests if since <= request.at < until],
            compactions=[event for event in session.compactions if since <= event.at < until],
            starts=[start for start in session.starts if start.at < until])
        if len(session.requests) < minimum:
            left_out += 1
            continue
        for request in session.requests:
            settings = _settings_at(session, request.at, timeline)
            if settings_label and settings_label != _settings_name(settings, timeline):
                continue
            group = _group_at(groups, settings, timeline, request.at, session_role, request.model, by)
            group.requests += 1
            if request.cli_version:
                group.versions.add(request.cli_version)
            starts = [start for start in session.starts if start.at <= request.at]
            if starts:
                version = max(starts, key=lambda start: start.at).cli_version
                if version:
                    group.versions.add(version)
        measurements = _measurements(session, active_gap, timeline=timeline, weekly=by == "week",
                                     starts=[start for start in session.starts if start.at >= since])
        for measurement in measurements:
            settings = _settings_at(session, measurement.at, timeline)
            if settings_label and settings_label != _settings_name(settings, timeline):
                continue
            group = _group_at(groups, settings, timeline, measurement.at, session_role, measurement.model, by)
            group.quantities[measurement.quantity].append(measurement.value)
            samples = role_samples.setdefault(session_role, {quantity: [] for quantity in QUANTITIES})
            samples[measurement.quantity].append(measurement.value)
    results = [GroupResult("; ".join(group.labels), group.values, ",".join(group.commits),
                           bucket if by == "week" else "", group_role, model, group.requests,
                           tuple(sorted(group.versions)),
                           {name: summarize(values) for name, values in group.quantities.items()})
               for (bucket, group_role, model), group in sorted(groups.items())]
    return Report(since, until, results, left_out,
                  {group_role: {name: summarize(values) for name, values in samples.items()}
                   for group_role, samples in role_samples.items()})


def _value(value: float, quantity: str) -> str:
    if quantity == "precomputed_share":
        return f"{100 * value:.1f}%"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _window(report: Report) -> str:
    zone = user_zone()
    return f"{report.since.astimezone(zone).isoformat()} to {report.until.astimezone(zone).isoformat()}"


def _week_marks(groups: Sequence[GroupResult]) -> dict[str, str]:
    seen_settings: set[str] = set()
    seen_versions: set[str] = set()
    seen_models: set[str] = set()
    marks: dict[str, list[str]] = defaultdict(list)
    for group in sorted(groups, key=lambda item: item.week):
        if not group.week:
            continue
        for commit, name in zip(group.settings_commit.split(","), group.settings_label.split("; ")):
            if commit not in seen_settings:
                marks[group.week].append("new settings: " + name)
                seen_settings.add(commit)
        for version in group.cli_versions:
            if version not in seen_versions:
                marks[group.week].append("new CLI: " + version)
                seen_versions.add(version)
        if group.model not in seen_models:
            marks[group.week].append("new model: " + group.model)
            seen_models.add(group.model)
    return {week: "; ".join(entries) for week, entries in marks.items()}


def render_report(report: Report) -> str:
    lines = ["Window: " + _window(report), f"left out: {report.left_out} sessions below min_requests"]
    marks = _week_marks(report.groups)
    weekly = any(group.week for group in report.groups)
    for quantity in QUANTITIES:
        lines.extend(("", "### " + quantity,
                      "| " + ("week | " if weekly else "") + "settings | role | model | n | median | mean | p25 | p75 |",
                      "| " + ("--- | " if weekly else "") + "--- | --- | --- | ---: | ---: | ---: | ---: | ---: |"))
        for group in report.groups:
            summary = group.quantities[quantity]
            values = [_value(value, quantity) if summary.n else "—"
                      for value in (summary.median, summary.mean, summary.p25, summary.p75)]
            columns = [group.settings_label, group.role, group.model, str(summary.n), *values]
            if weekly:
                columns.insert(0, group.week)
            lines.append("| " + " | ".join(_cell(column) for column in columns) + " |")
    for week, mark in marks.items():
        lines.append(f"{week}: {mark}")
    return "\n".join(lines)


def _summary_data(summary: QuantitySummary) -> dict[str, object]:
    return {"n": summary.n, "median": summary.median, "mean": summary.mean, "p25": summary.p25, "p75": summary.p75}


def save_result(name: str, report: Report, folder: Path) -> Path:
    """Write one result with exclusive creation; an existing result remains unchanged."""
    path = folder / (name + ".json")
    groups: list[dict[str, object]] = [{
        "settings_label": group.settings_label, "settings_values": group.settings_values,
        "settings_commit": group.settings_commit, "week": group.week, "role": group.role, "model": group.model,
        "requests": group.requests, "cli_versions": group.cli_versions,
        "quantities": {quantity: _summary_data(summary) for quantity, summary in group.quantities.items()},
    } for group in report.groups]
    data = {"name": name, "window": {"since": report.since.isoformat(), "until": report.until.isoformat()},
            "left_out": report.left_out, "groups": groups,
            "role_quantities": {role: {quantity: _summary_data(summary) for quantity, summary in quantities.items()}
                                for role, quantities in report.role_quantities.items()}}
    folder.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                           prefix="." + name + ".", suffix=".tmp", delete=False)
    temporary_path = Path(temporary.name)
    try:
        with temporary as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            _ = stream.write("\n")
        os.link(temporary_path, path)
    finally:
        temporary_path.unlink()
    return path


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


def _summary(value: object) -> QuantitySummary:
    data = _object(value)
    return QuantitySummary(cast(int, data["n"]), cast(float, data["median"]), cast(float, data["mean"]),
                           cast(float, data["p25"]), cast(float, data["p75"]))


def read_result(path: Path) -> Report:
    data = _object(cast(object, json.loads(path.read_text(encoding="utf-8"))))
    window = _object(data["window"])
    groups: list[GroupResult] = []
    for raw in cast(list[object], data["groups"]):
        group = _object(raw)
        groups.append(GroupResult(cast(str, group["settings_label"]), _object(group["settings_values"]),
            cast(str, group["settings_commit"]), cast(str, group["week"]), cast(str, group["role"]),
            cast(str, group["model"]), cast(int, group["requests"]), tuple(cast(list[str], group["cli_versions"])),
            {name: _summary(value) for name, value in _object(group["quantities"]).items()}))
    return Report(datetime.fromisoformat(cast(str, window["since"])), datetime.fromisoformat(cast(str, window["until"])),
                  groups, cast(int, data["left_out"]),
                  {role: {name: _summary(value) for name, value in _object(quantities).items()}
                   for role, quantities in _object(data["role_quantities"]).items()})


def _confounds(saved: Report, current: Report) -> list[str]:
    names = sorted({key for report in (saved, current) for group in report.groups for key in group.settings_values})
    differences: list[str] = []
    for name in names:
        before = {json.dumps(group.settings_values.get(name), sort_keys=True) for group in saved.groups}
        after = {json.dumps(group.settings_values.get(name), sort_keys=True) for group in current.groups}
        if before != after:
            differences.append(name)
    if {version for group in saved.groups for version in group.cli_versions} != {
            version for group in current.groups for version in group.cli_versions}:
        differences.append("Claude Code version")
    if {group.model for group in saved.groups} != {group.model for group in current.groups}:
        differences.append("model")
    return list(dict.fromkeys(differences))


def compare_reports(saved: Report, current: Report) -> str:
    lines = ["Saved window: " + _window(saved), "Current window: " + _window(current),
             "confounds: " + (", ".join(_confounds(saved, current)) or "none"), "",
             "| quantity | role | saved mean | current mean | change | saved n | current n |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    empty = QuantitySummary(0, 0, 0, 0, 0)
    for role in sorted(saved.role_quantities.keys() | current.role_quantities.keys()):
        for quantity in QUANTITIES:
            before = saved.role_quantities.get(role, {}).get(quantity, empty)
            after = current.role_quantities.get(role, {}).get(quantity, empty)
            if min(before.n, after.n) < 10:
                change = "too few"
            elif before.mean == 0:
                change = "0%" if after.mean == 0 else "undefined (saved zero)"
            else:
                change = f"{100 * (after.mean - before.mean) / before.mean:+.1f}%"
            lines.append("| " + " | ".join((quantity, _cell(role),
                _value(before.mean, quantity) if before.n else "—", _value(after.mean, quantity) if after.n else "—",
                change, str(before.n), str(after.n))) + " |")
    return "\n".join(lines)


def trend(folder: Path, role: str = "") -> str:
    reports = [(path.stem, read_result(path)) for path in folder.glob("*.json")]
    lines = ["| result | window | roles | requests | " + " | ".join(QUANTITIES) + " |",
             "| --- | --- | --- | ---: | " + " | ".join(["---"] * len(QUANTITIES)) + " |"]
    for name, report in sorted(reports, key=lambda entry: (entry[1].since, entry[0])):
        roles = sorted(group_role for group_role in report.role_quantities if not role or group_role == role)
        if not roles:
            continue
        quantities = ["; ".join(f"{group_role}: {_value(summary.mean, quantity)} (n={summary.n})"
                               if summary.n else f"{group_role}: — (n=0)"
                               for group_role in roles
                               for summary in [report.role_quantities[group_role][quantity]])
                      for quantity in QUANTITIES]
        count = sum(group.requests for group in report.groups if group.role in roles)
        lines.append("| " + " | ".join(_cell(column) for column in
            [name, _window(report), ", ".join(roles), str(count), *quantities]) + " |")
    return "\n".join(lines)


class _Arguments(argparse.Namespace):
    command: str = "report"
    name: str = ""
    since: str = ""
    until: str = ""
    role: str = ""
    by: Literal["settings", "week"] = "settings"
    settings: str = ""
    projects_root: Path
    store: Path
    checkout: Path
    saved_results: Path

    def __init__(self) -> None:
        super().__init__()
        self.projects_root = Path.home() / ".claude/projects"
        self.store = collect.store_path()
        self.checkout = configured_checkout()
        self.saved_results = state_dir() / "context_economy"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("report", "save", "compare", "trend"):
        subcommand = commands.add_parser(command)
        for path in ("projects-root", "store", "checkout", "saved-results"):
            _ = subcommand.add_argument("--" + path, type=Path, default=argparse.SUPPRESS)
        if command in ("save", "compare"):
            _ = subcommand.add_argument("name")
        if command != "trend":
            _ = subcommand.add_argument("--since", default="")
            _ = subcommand.add_argument("--until", default="")
        if command in ("report", "trend"):
            _ = subcommand.add_argument("--role", default="")
        if command == "report":
            _ = subcommand.add_argument("--by", choices=("settings", "week"), default="settings")
        if command == "save":
            _ = subcommand.add_argument("--settings", default="")
    return parser


def _bound(value: str, fallback: datetime) -> datetime:
    if not value:
        return fallback
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return at.replace(tzinfo=user_zone()) if at.tzinfo is None else at


def _timeline_start(sessions: Sequence[Session], since: datetime) -> datetime:
    anchors = [since]
    for session in sessions:
        earlier = [start.at for start in session.starts if start.at < since]
        if earlier:
            anchors.append(max(earlier))
        elif isinstance(session.first_record_at, datetime):
            anchors.append(session.first_record_at)
    return min(anchors)


def main(argv: Sequence[str] = ()) -> int:
    args = _parser().parse_args(argv, namespace=_Arguments())
    try:
        if args.command == "trend":
            print(trend(args.saved_results, args.role))
            return 0
        now = datetime.now(user_zone())
        since = _bound(args.since, now - timedelta(days=7))
        until = _bound(args.until, now)
        sessions = read_claude(args.projects_root, since, until, socket.gethostname())
        timeline = settings_timeline(args.checkout, _timeline_start(sessions, since), until)
        report = build_report(sessions, timeline, args.store, since, until,
                              by=args.by, role=args.role, settings_label=args.settings)
        if args.command == "save":
            print(save_result(args.name, report, args.saved_results))
        elif args.command == "compare":
            print(compare_reports(read_result(args.saved_results / (args.name + ".json")), report))
        else:
            print(render_report(report))
    except (OSError, ValueError) as error:
        print(f"context_economy: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
