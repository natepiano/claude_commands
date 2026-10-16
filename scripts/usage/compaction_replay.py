"""Compare worker compaction limits against stored requests in one read-only snapshot."""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from statistics import fmean, median
from typing import NoReturn, cast, override

from scripts.lib.user_zone import user_zone
from scripts.usage import collect, usage_report, weights


@dataclass(frozen=True)
class LimitRule:
    tokens: int


@dataclass(frozen=True)
class NeverRule:
    pass


@dataclass(frozen=True)
class NewWorkerRule:
    pass


Rule = LimitRule | NeverRule | NewWorkerRule
RULES: tuple[Rule, ...] = (LimitRule(100_000), LimitRule(150_000), LimitRule(200_000),
                          LimitRule(250_000), NeverRule(), NewWorkerRule())


@dataclass(frozen=True)
class Compacted:
    start: float


@dataclass(frozen=True)
class CarriedOn:
    start: float


@dataclass(frozen=True)
class NewWorker:
    start: float


BoundaryOutcome = Compacted | CarriedOn | NewWorker


@dataclass(frozen=True)
class FittedLine:
    intercept: float
    slope: float

    def kept(self, before: float) -> float:
        return max(0.0, self.intercept + self.slope * before)


@dataclass(frozen=True)
class FitUnavailable:
    reason: str


Fit = FittedLine | FitUnavailable


@dataclass(frozen=True)
class ThreadIdentity:
    machine: str
    parent_id: str
    agent_id: str


@dataclass(frozen=True)
class RecordedRequest:
    at: str
    key: str
    size: float
    cache_read: float
    output: float
    weighted: float
    cached_weight: float
    output_weight: float


@dataclass(frozen=True)
class Segment:
    requests: tuple[RecordedRequest, ...]


@dataclass(frozen=True)
class ReplayThread:
    identity: ThreadIdentity
    compactions: tuple[str, ...]
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class ReplayInputs:
    threads: tuple[ReplayThread, ...]
    no_compaction: int
    unweighted: int
    zero_rows: int


@dataclass(frozen=True)
class RuleResult:
    rule: Rule
    compactions: int
    new_workers: int
    weighted_tokens: float
    over_limit: int
    cache_floors: int


UNBOUNDED = usage_report.TimeWindow(usage_report.Unbounded(), usage_report.Unbounded())


def selection_sql(window: usage_report.TimeWindow) -> tuple[str, tuple[str, ...]]:
    """Select whole Codex threads, and compute weight coverage and omissions in SQL."""
    dated = weights.sql_weights("t.model", "t.at")
    predicate, parameters = window.predicate("first_at")
    query = f"""
        WITH weight_entries AS ({dated.entries}),
        thread_counts AS (
            SELECT t.machine, t.parent_id, t.agent_id,
                   coalesce(min(CASE WHEN t.input + t.cache_read > 0 THEN t.at END), min(t.at)) AS first_at,
                   sum(t.input + t.cache_read > 0) AS requests,
                   sum(t.input + t.cache_read = 0) AS zero_rows,
                   sum(t.input + t.cache_read > 0 AND w.weight_entry IS NULL) AS unweighted
            FROM turns t LEFT JOIN weight_entries w ON w.weight_entry = {dated.active_entry}
            WHERE t.family = 'codex' GROUP BY t.machine, t.parent_id, t.agent_id
        ), compaction_counts AS (
            SELECT machine, parent_id, agent_id, count(*) AS compactions
            FROM compactions WHERE family = 'codex' GROUP BY machine, parent_id, agent_id
        ), selected_threads AS (
            SELECT t.*, coalesce(c.compactions, 0) AS compactions
            FROM thread_counts t LEFT JOIN compaction_counts c
                USING (machine, parent_id, agent_id) WHERE {predicate}
        )
    """
    return query, parameters


def thread_identity(row: sqlite3.Row) -> ThreadIdentity:
    return ThreadIdentity(cast(str, row["machine"]), cast(str, row["parent_id"]), cast(str, row["agent_id"]))


def read_inputs(db: sqlite3.Connection, window: usage_report.TimeWindow) -> ReplayInputs:
    prefix, parameters = selection_sql(window)
    selected = tuple(cast(Iterable[sqlite3.Row], db.execute(prefix + "SELECT * FROM selected_threads", parameters)))
    no_compaction = sum(cast(int, row["compactions"]) == 0 for row in selected)
    unweighted = sum(cast(int, row["compactions"]) > 0 and cast(int, row["unweighted"]) > 0 for row in selected)
    zero_rows = sum(cast(int, row["zero_rows"]) for row in selected
                    if cast(int, row["compactions"]) > 0 and cast(int, row["unweighted"]) == 0
                    and cast(int, row["requests"]) > 0)
    requests: dict[ThreadIdentity, list[RecordedRequest]] = {}
    dated = weights.sql_weights("t.model", "t.at")
    query = prefix + f"""
        SELECT t.machine, t.parent_id, t.agent_id, t.at, t.turn_key,
               t.input + t.cache_read AS size, t.cache_read, t.output, w.cached,
               w.output AS output_weight, {usage_report.weighted_sql('t')} AS weighted
        FROM turns t JOIN selected_threads s USING (machine, parent_id, agent_id)
        JOIN weight_entries w ON w.weight_entry = {dated.active_entry}
        WHERE t.family = 'codex' AND t.input + t.cache_read > 0
            AND s.compactions > 0 AND s.unweighted = 0
        ORDER BY t.machine, t.parent_id, t.agent_id, t.at, t.turn_key
    """
    for row in cast(Iterable[sqlite3.Row], db.execute(query, parameters)):
        requests.setdefault(thread_identity(row), []).append(RecordedRequest(
            cast(str, row["at"]), cast(str, row["turn_key"]), float(cast(int, row["size"])),
            float(cast(int, row["cache_read"])), float(cast(int, row["output"])),
            float(cast(float, row["weighted"])), float(cast(float, row["cached"])),
            float(cast(float, row["output_weight"]))))
    compactions: dict[ThreadIdentity, list[str]] = {}
    query = prefix + """
        SELECT c.machine, c.parent_id, c.agent_id, c.at FROM compactions c
        JOIN selected_threads s USING (machine, parent_id, agent_id)
        WHERE c.family = 'codex' AND s.unweighted = 0 AND s.requests > 0
        ORDER BY c.machine, c.parent_id, c.agent_id, c.at
    """
    for row in cast(Iterable[sqlite3.Row], db.execute(query, parameters)):
        compactions.setdefault(thread_identity(row), []).append(cast(str, row["at"]))
    threads: list[ReplayThread] = []
    for identity, samples in requests.items():
        boundaries = tuple(compactions[identity])
        segments = tuple(Segment(tuple(group)) for _, group in groupby(
            samples, key=lambda sample: bisect_left(boundaries, sample.at)))
        threads.append(ReplayThread(identity, boundaries, segments))
    return ReplayInputs(tuple(threads), no_compaction, unweighted, zero_rows)


def fit_segments(threads: Sequence[ReplayThread]) -> Fit:
    pairs = [(left.requests[-1].size, right.requests[0].size)
             for thread in threads for left, right in zip(thread.segments, thread.segments[1:])]
    if not pairs:
        return FitUnavailable("no neighboring segments")
    before = fmean(pair[0] for pair in pairs)
    kept = fmean(pair[1] for pair in pairs)
    variance = math.fsum((x - before) ** 2 for x, _ in pairs)
    if variance == 0:
        return FitUnavailable("all before sizes are equal")
    slope = math.fsum((x - before) * (y - kept) for x, y in pairs) / variance
    return FittedLine(kept - slope * before, slope)


def rule_name(rule: Rule) -> str:
    if isinstance(rule, LimitRule):
        return f"limit {rule.tokens:,}"
    if isinstance(rule, NeverRule):
        return "never"
    return "new worker when kept is over 90,000 (estimate)"


def boundary_outcome(rule: Rule, fit: FittedLine, end: float,
                     first_size: float, prompt: float) -> BoundaryOutcome:
    if isinstance(rule, NeverRule) or end <= (rule.tokens if isinstance(rule, LimitRule) else 100_000):
        return CarriedOn(end + prompt)
    kept = fit.kept(end)
    if isinstance(rule, NewWorkerRule) and kept > 90_000:
        return NewWorker(first_size)
    return Compacted(kept)


def replay_rule(inputs: ReplayInputs, rule: Rule, fit: FittedLine,
                first_size: float, prompt: float) -> RuleResult:
    compacted = new_workers = over_limit = cache_floors = 0
    costs: list[float] = []
    for thread in inputs.threads:
        end = difference = 0.0
        for index, segment in enumerate(thread.segments):
            first = segment.requests[0]
            if index == 0:
                costs.extend(sample.weighted for sample in segment.requests)
            else:
                outcome = boundary_outcome(rule, fit, end, first_size, prompt)
                difference = outcome.start - first.size
                if isinstance(outcome, CarriedOn):
                    costs.append(end * first.cached_weight + prompt + first.output * first.output_weight)
                else:
                    costs.append(outcome.start + first.output * first.output_weight)
                    compacted += isinstance(outcome, Compacted)
                    new_workers += isinstance(outcome, NewWorker)
                for sample in segment.requests[1:]:
                    cache_floors += difference < -sample.cache_read
                    costs.append(sample.weighted + max(difference, -sample.cache_read) * sample.cached_weight)
            over_limit += sum(max(0.0, sample.size + difference) > 272_000 for sample in segment.requests)
            end = max(0.0, segment.requests[-1].size + difference)
    return RuleResult(rule, compacted, new_workers, math.fsum(costs), over_limit, cache_floors)


def render_replay(db: sqlite3.Connection, window: usage_report.TimeWindow = UNBOUNDED,
                  follow_up_prompt: float = 11_000.0) -> str:
    inputs = read_inputs(db, window)
    fit = fit_segments(inputs.threads)
    samples = [sample for thread in inputs.threads for segment in thread.segments for sample in segment.requests]
    lines = [f"Covered: {len(inputs.threads)} threads, "
             + f"{sum(len(thread.compactions) for thread in inputs.threads)} compactions, {len(samples)} requests."]
    median_line = "Median first-request size: unavailable."
    first_size = 0.0
    if inputs.threads:
        first_size = float(median(thread.segments[0].requests[0].size for thread in inputs.threads))
        median_line = f"Median first-request size: {first_size:,.0f} tokens."
    results: tuple[RuleResult, ...] = ()
    if isinstance(fit, FitUnavailable):
        lines.extend([median_line, "", f"fit unavailable: {fit.reason}", ""])
    else:
        lines.extend([f"Fit: kept = {fit.intercept:.6f} + {fit.slope:.6f} × before.", median_line, "",
                      "Replay excludes compaction-request cost and does not simulate Codex compaction of a full context.", ""])
        results = tuple(replay_rule(inputs, rule, fit, first_size, follow_up_prompt) for rule in RULES)
        baseline = results[0].weighted_tokens
        lines.extend(usage_report.markdown_table(
            ("rule", "simulated compactions", "new workers", "weighted tokens",
             "change against limit 100,000", "requests over 272,000"),
            ([rule_name(result.rule), str(result.compactions), str(result.new_workers),
              f"{result.weighted_tokens:,.0f}",
              f"{result.weighted_tokens / baseline - 1:+.1%}" if baseline else "—",
              str(result.over_limit)] for result in results)))
        stored = math.fsum(sample.weighted for sample in samples)
        lines.extend(["", f"Stored weighted tokens: {stored:,.0f}; limit 100,000 replay: {baseline:,.0f}."])
    lines.extend([f"Threads left out: no compaction={inputs.no_compaction}; request with no weight entry={inputs.unweighted}.",
                  f"Rows of size 0 skipped: {inputs.zero_rows}."])
    floors = [result for result in results if result.cache_floors]
    if floors:
        lines.append("Requests with simulated cache read held at 0:")
        lines.extend(f"- {rule_name(result.rule)}: {result.cache_floors}." for result in floors)
    else:
        lines.append("Requests with simulated cache read held at 0: none.")
    return "\n".join(lines) + "\n"


class ReplayParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        raise usage_report.ReportRefused(message)


class ReplayArguments(argparse.Namespace):
    db: Path
    since: str
    until: str
    follow_up_prompt: float

    def __init__(self) -> None:
        super().__init__()
        self.db = collect.store_path()
        self.since = ""
        self.until = ""
        self.follow_up_prompt = 11_000.0


def main(argv: Sequence[str] = ()) -> int:
    parser = ReplayParser(description=__doc__)
    _ = parser.add_argument("--db", type=Path)
    _ = parser.add_argument("--since")
    _ = parser.add_argument("--until")
    _ = parser.add_argument("--follow-up-prompt", type=float)
    try:
        args = parser.parse_args(argv, namespace=ReplayArguments())
        zone = user_zone()
        window = usage_report.TimeWindow(usage_report.parse_bound(args.since, zone),
                                         usage_report.parse_bound(args.until, zone))
        if (isinstance(window.since, usage_report.TimeBound) and isinstance(window.until, usage_report.TimeBound)
                and window.since.at >= window.until.at):
            raise usage_report.ReportRefused("invalid bounds; accepted: --since earlier than --until (inclusive start, exclusive end)")
        if not math.isfinite(args.follow_up_prompt) or args.follow_up_prompt < 0:
            raise usage_report.ReportRefused("follow-up prompt must be a finite nonnegative token count")
        with closing(usage_report.open_read_store(args.db)) as db:
            usage_report.check_store_version(db)
            print(render_replay(db, window, args.follow_up_prompt), end="")
    except usage_report.ReportRefused as error:
        print("compaction_replay: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error, usage_report.StoreVersionMismatch) as error:
        print("compaction_replay: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
