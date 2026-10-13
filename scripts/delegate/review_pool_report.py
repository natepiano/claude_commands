#!/usr/bin/env python3
"""Compare pooled and own-seat review costs and phase finding quality."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Literal, TypedDict, cast

# Sibling scripts are also executable directly from this directory.
REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY))
from scripts.production import review_regime
from scripts.usage import collect, usage_report, weights
from scripts.whoami.seat_usage import parse_boundary

Arm = Literal["pool", "own"]
Kind = Literal["broad", "closure", "ux"]
Outcome = Literal["reviewed", "void", "canceled", "error"]
RecordKey = tuple[str, str]
PhaseKey = tuple[str, str, str]
HistoryKey = tuple[str, str]


class ReviewTurnJson(TypedDict):
    arm: Arm
    pool: str
    reviewer: str
    thread_id: str
    unit: str
    phase: str
    phase_instance_id: str
    history_file: str
    kind: Kind
    lens: str
    started_at: int
    finished_at: float
    lease_ordinal: int
    prime_hash: str
    status: Outcome
    raised: int


@dataclass(frozen=True)
class ReviewRecord:
    machine: str
    path: str
    arm: Arm
    kind: Kind
    outcome: Outcome
    thread_id: str
    unit: str
    phase: str
    phase_instance_id: str
    history_file: str
    started_at: float
    finished_at: float
    lease_ordinal: int
    raised: int

    @property
    def key(self) -> RecordKey:
        return self.machine, self.path

    @property
    def phase_key(self) -> PhaseKey:
        # Missing phase metadata must not join unrelated reviews together.
        return self.machine, self.history_file, self.phase_instance_id or self.path

    def contains(self, at: float) -> bool:
        return self.started_at <= at <= self.finished_at


@dataclass(frozen=True)
class ReviewRequest:
    at: datetime
    model: str
    tokens: collect.RawTokens

    def token_row(self) -> dict[str, object]:
        return {"model": self.model, "input": self.tokens.input,
                "cache_read": self.tokens.cache_read, "output": self.tokens.output,
                "cache_write_5m": self.tokens.cache_write_5m,
                "cache_write_1h": self.tokens.cache_write_1h}


@dataclass
class CodexThreadUsage:
    agent: collect.CodexThread
    requests: list[ReviewRequest] = field(default_factory=list)
    compactions: list[datetime] = field(default_factory=list)


@dataclass(frozen=True)
class CompleteCost:
    weighted_tokens: float
    weighted_cached_input: float
    began_with_compaction: bool


@dataclass(frozen=True)
class IncompleteCost:
    reason: str


ReviewCost = CompleteCost | IncompleteCost


@dataclass(frozen=True)
class AvailableUsageStore:
    """The shared store was read completely."""


@dataclass(frozen=True)
class UnavailableUsageStore:
    """Review costs cannot be read from the shared store."""


UsageStore = AvailableUsageStore | UnavailableUsageStore
AVAILABLE_USAGE_STORE = AvailableUsageStore()


@dataclass(frozen=True)
class UXDefect:
    shot: str
    rule: str
    defect: str
    fix: str

    @property
    def pairs(self) -> tuple[tuple[str, str], ...]:
        rule = self.rule.replace("`", "").strip().lower().removesuffix(".md")
        return tuple((rule, Path(shot.replace("`", "").strip()).name) for shot in self.shot.split(","))

    def table_row(self) -> str:
        return f"| {self.shot} | {self.rule} | {self.defect} | {self.fix} |"


@dataclass(frozen=True)
class PassUXVerdict:
    @property
    def rows(self) -> tuple[UXDefect, ...]:
        return ()


@dataclass(frozen=True)
class DefectsUXVerdict:
    first: UXDefect
    remaining: tuple[UXDefect, ...]

    @property
    def rows(self) -> tuple[UXDefect, ...]:
        return (self.first, *self.remaining)


@dataclass(frozen=True)
class UnusableUXVerdict:
    reason: str


UXVerdict = PassUXVerdict | DefectsUXVerdict | UnusableUXVerdict


@dataclass
class ReportSources:
    records: list[ReviewRecord] = field(default_factory=list)
    threads: list[CodexThreadUsage] = field(default_factory=list)
    histories: dict[HistoryKey, list[dict[str, object]]] = field(default_factory=dict)
    holds: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    usage_store: UsageStore = field(default_factory=AvailableUsageStore)


def json_object(text: str) -> dict[str, object]:
    value = cast(object, json.loads(text))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, object], value)


def read_record(text: str, machine: str, path: str) -> ReviewRecord:
    values = json_object(text)
    for key in ("arm", "kind", "status", "thread_id", "unit", "phase", "phase_instance_id", "history_file"):
        if not isinstance(values.get(key), str):
            raise ValueError(f"missing or invalid {key}")
    for key in ("started_at", "finished_at"):
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"missing or invalid {key}")
    for key in ("lease_ordinal", "raised"):
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"missing or invalid {key}")
    data = cast(ReviewTurnJson, cast(object, values))
    if data["arm"] not in ("pool", "own") or data["kind"] not in ("broad", "closure", "ux"):
        raise ValueError("unknown arm or kind")
    if data["status"] not in ("reviewed", "void", "canceled", "error"):
        raise ValueError("unknown status")
    if data["finished_at"] < data["started_at"]:
        raise ValueError("finished_at precedes started_at")
    return ReviewRecord(machine, path, data["arm"], data["kind"], data["status"],
                        data["thread_id"], data["unit"], data["phase"], data["phase_instance_id"],
                        data["history_file"], data["started_at"], data["finished_at"],
                        data["lease_ordinal"], data["raised"])


def read_records(root: Path) -> list[ReviewRecord]:
    records: list[ReviewRecord] = []
    for path in sorted(root.glob("review_turns/*/*.json")):
        try:
            record = read_record(path.read_text(encoding="utf-8"), "local", str(path))
        except (OSError, ValueError) as exc:
            print(f"review_pool_report: skipped local:{path}: {exc}", file=sys.stderr)
            continue
        records.append(record)
    return records


def stored_usage(sources: ReportSources, include_mac: bool) -> None:
    wanted = {(record.machine, record.thread_id) for record in sources.records if record.thread_id}
    threads: dict[tuple[str, str], CodexThreadUsage] = {}

    def thread_for(row: sqlite3.Row) -> CodexThreadUsage:
        machine = cast(str, row["machine"])
        # The local collector calls this machine natedev; records call it local.
        machine = "local" if machine == "natedev" else machine
        thread_id = cast(str, row["agent_id"])
        key = machine, thread_id
        if key not in threads:
            threads[key] = CodexThreadUsage(collect.CodexThread(machine, thread_id))
        return threads[key]

    def selected(row: sqlite3.Row) -> bool:
        machine = cast(str, row["machine"])
        machine = "local" if machine == "natedev" else machine
        return (include_mac or machine != "mac") and (machine, cast(str, row["agent_id"])) in wanted

    try:
        with closing(usage_report.open_read_store(collect.store_path())) as db:
            # Metadata-only threads still distinguish missing usage from a missing thread.
            identities = db.execute(
                "SELECT machine, agent_id FROM openings WHERE family = 'codex' "
                + "UNION SELECT machine, agent_id FROM folders WHERE family = 'codex'")
            for row in cast(Iterable[sqlite3.Row], identities):
                if selected(row):
                    _ = thread_for(row)
            rows = db.execute("SELECT * FROM turns WHERE family = 'codex' ORDER BY at, turn_key")
            for row in cast(Iterable[sqlite3.Row], rows):
                if not selected(row):
                    continue
                thread = thread_for(row)
                tokens = collect.RawTokens(*(cast(int, row[column]) for column in
                                             ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")))
                if tokens == collect.RawTokens(0, 0, 0, 0, 0):
                    continue
                thread.requests.append(ReviewRequest(
                    parse_boundary(cast(str, row["at"])), cast(str, row["model"]), tokens))
            rows = db.execute("SELECT * FROM compactions WHERE family = 'codex' ORDER BY at")
            for row in cast(Iterable[sqlite3.Row], rows):
                if selected(row):
                    thread_for(row).compactions.append(parse_boundary(cast(str, row["at"])))
            if include_mac:
                rows = db.execute("SELECT state, last_complete_at FROM turn_sources "
                                  + "WHERE machine = 'mac' AND family = 'codex'")
                for row in cast(Iterable[sqlite3.Row], rows):
                    if row["state"] != "ready":
                        sources.warnings.append(f"Mac usage source {row['state']}; last complete read {row['last_complete_at']}")
    except (OSError, ValueError, sqlite3.Error) as exc:
        sources.warnings.append(f"usage store unavailable: {exc}")
        sources.usage_store = UnavailableUsageStore()
        return
    sources.threads = list(threads.values())
    sources.usage_store = AvailableUsageStore()


def attribute_costs(records: Sequence[ReviewRecord], threads: Sequence[CodexThreadUsage],
                    usage_store: UsageStore = AVAILABLE_USAGE_STORE) -> dict[RecordKey, ReviewCost]:
    owners: dict[tuple[str, str], list[ReviewRecord]] = {}
    for record in sorted(records, key=lambda item: (item.started_at, item.path)):
        owners.setdefault((record.machine, record.thread_id), []).append(record)
    requests: dict[RecordKey, list[ReviewRequest]] = {record.key: [] for record in records}
    found: set[tuple[str, str]] = set()
    compactions: dict[RecordKey, list[float]] = {}
    for thread in threads:
        thread_key = thread.agent.machine, thread.agent.agent_id
        found.add(thread_key)
        candidates = owners.get(thread_key, [])
        for request in thread.requests:
            for record in candidates:
                if record.contains(request.at.timestamp()):
                    requests[record.key].append(request)
                    break
        for at in thread.compactions:
            for record in candidates:
                if record.contains(at.timestamp()):
                    compactions.setdefault(record.key, []).append(at.timestamp())
    costs: dict[RecordKey, ReviewCost] = {}
    for record in records:
        if isinstance(usage_store, UnavailableUsageStore):
            costs[record.key] = IncompleteCost("usage store unavailable")
            continue
        owned = requests[record.key]
        markers = compactions.get(record.key, [])
        if (record.machine, record.thread_id) not in found:
            costs[record.key] = IncompleteCost("missing rollout")
        elif not owned:
            costs[record.key] = IncompleteCost("no request in its window")
        else:
            first_request = min(request.at.timestamp() for request in owned)
            if any(at > first_request for at in markers):
                costs[record.key] = IncompleteCost("a compaction after its first request")
                continue
            total = cached = 0.0
            for request in owned:
                try:
                    entry = weights.weights_at(request.model, request.at)
                    total += weights.weighted(request.token_row(), request.at)
                    cached += request.tokens.cache_read * entry.cached
                except weights.UnweightedModel:
                    costs[record.key] = IncompleteCost(f"unweighted model {request.model}")
                    break
            else:
                costs[record.key] = CompleteCost(
                    total, cached, began_with_compaction=any(at < first_request for at in markers))
    return costs


def read_events(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    events: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                events.append(json_object(line))
    return events


def local_sources(root: Path, quality: bool) -> ReportSources:
    # Earlier records still own requests in overlapping windows at the cutoff.
    sources = ReportSources(records=read_records(root))
    if quality:
        for path in {record.history_file for record in sources.records if record.history_file}:
            try:
                sources.histories[("local", path)] = read_events(Path(path))
            except (OSError, ValueError) as exc:
                sources.warnings.append(f"history unavailable: {path}: {exc}")
        try:
            sources.holds["local"] = read_events(review_regime.LEDGER)
        except (OSError, ValueError) as exc:
            sources.warnings.append(f"merge holds unavailable: {exc}")
    return sources


def remote_command(root: str, quality: bool) -> str:
    # Codex usage comes from the store; SSH carries review records and quality events.
    script = '''import json, os, sys
from pathlib import Path
root = Path(sys.argv[1] or os.environ.get('PLAN_DELEGATE_HISTORY_DIR', '~/.local/state/plan-delegate')).expanduser()
quality = sys.argv[2] == '1'
histories = set()
def emit(kind, path):
 try:
  text = path.read_text(encoding='utf-8', errors='replace')
  print(json.dumps({'kind': kind, 'path': str(path), 'text': text}))
  return text
 except OSError as error:
  print(json.dumps({'kind': 'warning', 'path': str(path), 'text': str(error)}))
  return ''
for path in sorted(root.glob('review_turns/*/*.json')):
 text = emit('record', path)
 try:
  record = json.loads(text)
  if quality:
   history = record.get('history_file', '')
   if history: histories.add(history)
 except ValueError: pass
if quality:
 for history in sorted(histories): emit('history', Path(history))
 path = Path.home() / '.claude/data/review_regime.jsonl'
 if path.exists(): emit('holds', path)
'''
    return shlex.join(("python3", "-c", script, root, "1" if quality else "0"))


def mac_sources(root: str, quality: bool) -> ReportSources:
    sources = ReportSources()
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", collect.MAC_HOST, remote_command(root, quality)]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        try:
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors,
                                       text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            sources.warnings.append(f"Mac unavailable: {exc}")
            return sources
        output: IO[str] | None = process.stdout
        if output is None:
            _ = process.wait()
            sources.warnings.append("Mac unavailable: ssh produced no stream")
            return sources
        for line in output:
            try:
                envelope = json_object(line)
                kind, path, text = (envelope[key] for key in ("kind", "path", "text"))
                if not isinstance(path, str) or not isinstance(text, str):
                    raise ValueError("invalid remote envelope")
                if kind == "record":
                    sources.records.append(read_record(text, "mac", path))
                elif kind in ("history", "holds"):
                    events = [json_object(item) for item in text.splitlines() if item.strip()]
                    if kind == "history":
                        sources.histories[("mac", path)] = events
                    else:
                        sources.holds["mac"] = events
                elif kind == "warning":
                    sources.warnings.append(f"Mac file unavailable: {path}: {text}")
            except (KeyError, ValueError) as exc:
                sources.warnings.append(f"invalid Mac file: {exc}")
        output.close()
        returncode = process.wait()
        if returncode:
            _ = errors.seek(0)
            return ReportSources(warnings=[f"Mac unavailable: {(errors.read().strip() or f'ssh exited {returncode}')}"])
    return sources


def cost_cells(records: Sequence[ReviewRecord], costs: Mapping[RecordKey, ReviewCost]) -> str:
    complete = [cost for record in records if isinstance(cost := costs[record.key], CompleteCost)]
    values = [cost.weighted_tokens for cost in complete]
    total = sum(values)
    cached = sum(cost.weighted_cached_input for cost in complete)
    median = f"{statistics.median(values):.1f}" if values else "-"
    mean = f"{statistics.mean(values):.1f}" if values else "-"
    share = f"{cached / total * 100:.1f}%" if total else "-"
    return f"{len(records)} | {median} | {mean} | {share}"


def quality_lines(records: Sequence[ReviewRecord], sources: ReportSources) -> list[str]:
    phases: dict[PhaseKey, list[ReviewRecord]] = {}
    selected_phases = {record.phase_key for record in records if record.kind != "ux"}
    for record in sources.records:
        if record.kind != "ux" and record.phase_key in selected_phases:
            phases.setdefault(record.phase_key, []).append(record)
    mixed = {key for key, group in phases.items() if len({record.arm for record in group}) > 1}
    lines = ["Code review quality by arm", "| Arm | Phases | Raised | Confirmed | Precision | Escapes per phase |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for arm in ("pool", "own"):
        groups = [group for key, group in phases.items() if key not in mixed and group[0].arm == arm]
        raised = confirmed = escapes = 0
        for group in groups:
            record = group[0]
            raised += sum(item.raised for item in group if item.outcome == "reviewed")
            events = sources.histories.get((record.machine, record.history_file), [])
            for event in events:
                if (event.get("event_type") != "finding_opened" or
                        event.get("phase_instance_id") != record.phase_instance_id or event.get("lens") == "ux"):
                    continue
                if event.get("caught_by") in ("delegate", "both"):
                    confirmed += 1
                elif event.get("caught_by") == "main":
                    escapes += 1
            # Merge holds describe the whole phase, not a particular lens.
            escapes += sum(int(value) for row in sources.holds.get(record.machine, [])
                           if row.get("unit") == record.unit and str(row.get("phase")) == record.phase
                           and isinstance(value := row.get("holds"), int))
        precision = f"{confirmed / raised * 100:.1f}%" if raised else "-"
        escape_rate = f"{escapes / len(groups):.1f}" if groups else "-"
        lines.append(f"| {arm} | {len(groups)} | {raised} | {confirmed} | {precision} | {escape_rate} |")
    lines.extend((f"mixed phases: {len(mixed)} (excluded from comparison)",
                  "ui checks: compared by --shadow, not in this table"))
    return lines


def render_report(records: Sequence[ReviewRecord], costs: Mapping[RecordKey, ReviewCost],
                  quality: bool, *, sources: ReportSources) -> str:
    reviewed = [record for record in records if record.outcome == "reviewed"]
    lines = ["Tokens by arm and kind", "| Arm | Kind | Reviews | Median weighted tokens | Mean weighted tokens | Cached input share |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    for arm in ("pool", "own"):
        for kind in ("broad", "closure", "ux"):
            group = [record for record in reviewed if record.arm == arm and record.kind == kind]
            lines.append(f"| {arm} | {kind} | {cost_cells(group, costs)} |")
    lines += ["", "Pool tokens by lease ordinal", "| Lease ordinal | Reviews | Median weighted tokens | Mean weighted tokens | Cached input share |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for label in ("1", "2", "3+"):
        group = [record for record in reviewed if record.arm == "pool" and
                 (str(record.lease_ordinal) == label or (label == "3+" and record.lease_ordinal >= 3))]
        lines.append(f"| {label} | {cost_cells(group, costs)} |")
    lines.append("")
    for arm in ("pool", "own"):
        counts = [sum(record.arm == arm and record.outcome == outcome for record in records)
                  for outcome in ("void", "canceled", "error")]
        lines.append(f"{arm} outcomes: void {counts[0]}; canceled {counts[1]}; error {counts[2]}")
    incomplete = [(record, cost) for record in reviewed if isinstance(cost := costs[record.key], IncompleteCost)]
    details = "; ".join(f"{record.machine}:{record.path} ({cost.reason})" for record, cost in incomplete)
    lines.append(f"cost incomplete: {len(incomplete)}" + (f": {details}" if details else ""))
    began_with_compaction = {
        arm: sum(record.arm == arm and isinstance(cost := costs[record.key], CompleteCost)
                 and cost.began_with_compaction for record in reviewed)
        for arm in ("pool", "own")
    }
    lines.append(f"began with a compaction: pool {began_with_compaction['pool']}; own {began_with_compaction['own']}")
    if quality:
        lines.extend(("", *quality_lines(records, sources)))
    lines.extend(f"source warning: {warning}" for warning in sources.warnings)
    code_reviews = [record for record in reviewed if record.kind in ("broad", "closure")]
    pool_count = sum(record.arm == "pool" for record in code_reviews)
    if pool_count < 20:
        lines.append(f"pool reviews: {pool_count} of 20 for the first reading")
    else:
        means: dict[str, float] = {}
        for arm in ("pool", "own"):
            values = [cost.weighted_tokens for record in code_reviews if record.arm == arm
                      and isinstance(cost := costs[record.key], CompleteCost)]
            if values:
                means[arm] = statistics.mean(values)
        if "pool" in means and means.get("own", 0) > 0:
            lines.append(f"pool is at {means['pool'] / means['own'] * 100:.1f}% of own-seat tokens per review")
        else:
            lines.append("pool tokens per review: no comparison yet (complete costs needed for both arms)")
    return "\n".join(lines)


def ux_table_cells(line: str) -> list[str]:
    cells = [" ".join(cell.split()) for cell in re.split(r"(?<!\\)\|", line.strip())[1:]]
    if cells and not cells[-1]:
        _ = cells.pop()
    return cells


def read_ux_verdict(path: Path) -> UXVerdict:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return UnusableUXVerdict(str(exc))
    verdicts: list[str] = []
    for line in lines:
        text = line.replace("*", "").strip()
        match = re.fullmatch(r"Verdict:\s*(pass|defects)", text, re.IGNORECASE)
        if match:
            verdicts.append(match[1].lower())
    if not verdicts:
        return UnusableUXVerdict("missing verdict line")
    if len(verdicts) != 1:
        return UnusableUXVerdict("multiple verdict lines")
    headers = [index for index, line in enumerate(lines)
               if line.strip().startswith("|") and ux_table_cells(line) == ["Shot", "Rule", "Defect", "Fix"]]
    if not headers:
        return UnusableUXVerdict("missing defect table")
    if len(headers) != 1:
        return UnusableUXVerdict("multiple defect tables")
    start = headers[0] + 1
    if start >= len(lines):
        return UnusableUXVerdict("missing defect table separator")
    separator = ux_table_cells(lines[start])
    if len(separator) != 4 or not all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in separator):
        return UnusableUXVerdict("invalid defect table separator")
    defects: list[UXDefect] = []
    for line in lines[start + 1:]:
        if not line.strip().startswith("|"):
            break
        cells = ux_table_cells(line)
        if len(cells) != 4 or not all(cells):
            return UnusableUXVerdict("invalid defect table row")
        if all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in cells):
            continue
        defects.append(UXDefect(*cells))
    if verdicts[0] == "pass":
        return UnusableUXVerdict("pass verdict has defect rows") if defects else PassUXVerdict()
    if not defects:
        return UnusableUXVerdict("defects verdict has no defect rows")
    return DefectsUXVerdict(defects[0], tuple(defects[1:]))


def render_shadow(root: Path) -> str:
    checks: dict[tuple[Path, str], dict[str, Path]] = {}
    for path in sorted(root.glob("review_turns/*/ux_*.txt")):
        match = re.fullmatch(r"ux_(.+)_([1-9][0-9]*)_(helper|pool)\.txt", path.name)
        if match:
            check = f"ux_{match[1]}_{match[2]}"
            checks.setdefault((path.parent, check), {})[match[3]] = path
    lines = ["UI shadow checks (all local pairs; --since and --quality do not filter them)",
             "Comparison: rule and shot file name, by count; Defect and Fix wording is not compared."]
    complete = incomplete = missed_total = 0
    for (directory, check), pair in checks.items():
        lines.extend(("", f"check: {directory.name}/{check}"))
        if len(pair) != 2:
            incomplete += 1
            for arm in ("helper", "pool"):
                if arm not in pair:
                    lines.append(f"unpaired file: {next(iter(pair.values()))}; missing {directory / f'{check}_{arm}.txt'}")
            continue
        helper = read_ux_verdict(pair["helper"])
        pool = read_ux_verdict(pair["pool"])
        for arm, verdict in (("helper", helper), ("pool", pool)):
            if isinstance(verdict, UnusableUXVerdict):
                lines.append(f"verdict unavailable: {pair[arm]}: {verdict.reason}")
        if isinstance(helper, UnusableUXVerdict) or isinstance(pool, UnusableUXVerdict):
            incomplete += 1
            continue
        available = Counter(pair for row in pool.rows for pair in row.pairs)
        missed: list[UXDefect] = []
        for row in helper.rows:
            needed = Counter(row.pairs)
            if all(available[pair] >= count for pair, count in needed.items()):
                available.subtract(needed)
            else:
                missed.append(row)
        complete += 1
        missed_total += len(missed)
        for label, rows in (("helper", helper.rows), ("pool", pool.rows), ("pool missed", missed)):
            lines.extend((f"{label} defects: {len(rows)}", "| Shot | Rule | Defect | Fix |",
                          "| --- | --- | --- | --- |"))
            lines.extend(row.table_row() for row in rows)
    lines.extend(("", f"complete shadow checks: {complete}; incomplete: {incomplete}; pool missed rows: {missed_total}",
                  "Adoption is the user's decision after ten checks; this report creates no marker."))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--since")
    _ = parser.add_argument("--root")
    _ = parser.add_argument("--no-mac", action="store_true")
    _ = parser.add_argument("--quality", action="store_true")
    _ = parser.add_argument("--shadow", action="store_true", help="compare local UI verdict pairs without the usage store")
    args = parser.parse_args()
    shadow = cast(bool, args.shadow)
    since_text = cast(str | None, args.since)
    if not shadow and not since_text:
        parser.error("--since is required unless --shadow is used")
    try:
        configured_root = cast(str | None, args.root)
        root = Path(configured_root or os.environ.get("PLAN_DELEGATE_HISTORY_DIR", "~/.local/state/plan-delegate")).expanduser()
        if shadow:
            print(render_shadow(root))
            return 0
        since = parse_boundary(cast(str, since_text))
        quality = cast(bool, args.quality)
        sources = local_sources(root, quality)
        include_mac = not cast(bool, args.no_mac)
        if include_mac:
            mac = mac_sources(configured_root or "", quality)
            sources.records.extend(mac.records)
            sources.histories.update(mac.histories)
            sources.holds.update(mac.holds)
            sources.warnings.extend(mac.warnings)
        stored_usage(sources, include_mac)
        costs = attribute_costs(sources.records, sources.threads, sources.usage_store)
        selected = [record for record in sources.records if record.started_at >= since.timestamp()]
        print(render_report(selected, costs, quality, sources=sources))
    except (OSError, ValueError) as exc:
        print(f"review_pool_report: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
