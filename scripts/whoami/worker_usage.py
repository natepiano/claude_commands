#!/usr/bin/env python3
"""Compare Codex usage per checkpointed phase before and after long-lived workers."""

from __future__ import annotations

import argparse
import math
import sys
import sqlite3
from contextlib import closing
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from enum import Enum
from zoneinfo import ZoneInfo
from typing import cast

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.usage import collect, usage_report, weights
from scripts.whoami.note_file import NumberedAccountLabel, canonical_label, parse_account_label

DEFAULT_T_LIVE = datetime(2026, 10, 10, 18, 17, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("window boundaries must include a timezone")
        if self.start >= self.end:
            raise ValueError("window start must be before its end")

@dataclass(frozen=True)
class WindowPair:
    before: Window
    after: Window


@dataclass(frozen=True)
class KnownRate:
    value: float


@dataclass(frozen=True)
class UnavailableRate:
    pass


Rate = KnownRate | UnavailableRate


@dataclass(frozen=True)
class KnownCreditRate:
    per_day: float


@dataclass(frozen=True)
class NoCreditHistory:
    pass


@dataclass(frozen=True)
class InsufficientCreditHistory:
    pass


CreditRate = KnownCreditRate | NoCreditHistory | InsufficientCreditHistory


@dataclass(frozen=True)
class KnownSwitchRate:
    per_week: float


@dataclass(frozen=True)
class SwitchHistoryUnavailable:
    pass


SwitchRate = KnownSwitchRate | SwitchHistoryUnavailable


@dataclass(frozen=True)
class KnownCreditBalance:
    value: float


@dataclass(frozen=True)
class UnknownCreditBalance:
    pass


CreditBalance = KnownCreditBalance | UnknownCreditBalance


@dataclass(frozen=True)
class AccountReading:
    account: str
    at: datetime
    credits: CreditBalance


@dataclass(frozen=True)
class DefaultStore:
    pass


class CommandLine(argparse.Namespace):
    before: list[str] | None
    after: list[str] | None
    daily: bool
    t_live: str | None
    db: Path | DefaultStore

    def __init__(self) -> None:
        super().__init__()
        self.before = None
        self.after = None
        self.daily = False
        self.t_live = None
        self.db = DefaultStore()


def parse_boundary(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid ISO date or datetime: {value}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def window_from(values: Sequence[str]) -> Window:
    if len(values) != 2:
        raise ValueError("a window needs a start and end")
    return Window(parse_boundary(values[0]), parse_boundary(values[1]))


def credit_balance(value: object) -> CreditBalance:
    if isinstance(value, bool):
        return UnknownCreditBalance()
    if isinstance(value, (int, float)):
        balance = float(value)
        return KnownCreditBalance(balance) if math.isfinite(balance) and balance >= 0 else UnknownCreditBalance()
    if isinstance(value, str):
        try:
            balance = float(value)
        except ValueError:
            return UnknownCreditBalance()
        return KnownCreditBalance(balance) if math.isfinite(balance) and balance >= 0 else UnknownCreditBalance()
    return UnknownCreditBalance()


@dataclass(frozen=True)
class HistoryBeginsAt:
    at: datetime


@dataclass(frozen=True)
class NoStoredHistory:
    pass


HistoryStart = HistoryBeginsAt | NoStoredHistory


@dataclass(frozen=True)
class RoleSwitchHistory:
    start: HistoryStart
    switches: tuple[datetime, ...]


def store_switch_history(db: sqlite3.Connection) -> RoleSwitchHistory:
    from scripts.accounts import accounts

    row = cast(sqlite3.Row, db.execute("SELECT min(at) FROM account_role_changes").fetchone())
    start = cast(object, row[0])
    if not isinstance(start, str):
        return RoleSwitchHistory(NoStoredHistory(), ())
    rows = cast(Iterable[sqlite3.Row], db.execute(
        "SELECT machine, at, label FROM account_role_changes WHERE role = ? ORDER BY at, label",
        (accounts.CODEX_ROLE,)))
    previous: dict[str, str] = {}
    switches: list[datetime] = []
    for row in rows:
        machine = cast(str, row["machine"])
        label = canonical_label(cast(str, row["label"]).casefold())
        if machine in previous and previous[machine] != label:
            switches.append(datetime.fromisoformat(cast(str, row["at"])))
        previous[machine] = label
    return RoleSwitchHistory(HistoryBeginsAt(datetime.fromisoformat(start)), tuple(switches))



NO_ROLE_HISTORY = RoleSwitchHistory(NoStoredHistory(), ())


def switches_per_week(readings: Iterable[AccountReading], window: Window,
                      history: RoleSwitchHistory = NO_ROLE_HISTORY) -> SwitchRate:
    ordered = sorted((reading for reading in readings if reading.at < window.end), key=lambda item: item.at)
    prior = [reading for reading in ordered if reading.at < window.start]
    within = [reading for reading in ordered if reading.at >= window.start]
    if not within:
        return SwitchHistoryUnavailable()
    coverage_start = window.start if prior else within[0].at
    coverage_end = within[-1].at
    if coverage_end <= coverage_start:
        return SwitchHistoryUnavailable()
    previous = prior[-1] if prior else within[0]
    observations = within if prior else within[1:]
    switches = 0
    for reading in observations:
        before_history = (isinstance(history.start, NoStoredHistory)
                          or previous.at < history.start.at and reading.at < history.start.at)
        if before_history and reading.account != previous.account:
            switches += 1
        previous = reading
    switches += sum(coverage_start <= at <= coverage_end for at in history.switches)
    coverage_days = (coverage_end - coverage_start).total_seconds() / 86_400
    return KnownSwitchRate(switches * 7.0 / coverage_days)


def credits_per_day(readings: Iterable[AccountReading], window: Window) -> CreditRate:
    by_account: dict[str, list[AccountReading]] = {}
    for reading in readings:
        if isinstance(reading.credits, KnownCreditBalance) and reading.at < window.end:
            by_account.setdefault(reading.account, []).append(reading)
    used = 0.0
    coverage_start = window.end
    coverage_end = window.start
    measured = False
    samples = 0
    for account_readings in by_account.values():
        account_readings.sort(key=lambda reading: reading.at)
        samples += len(account_readings)
        for previous, reading in zip(account_readings, account_readings[1:], strict=False):
            if reading.at < window.start:
                continue
            assert isinstance(previous.credits, KnownCreditBalance)
            assert isinstance(reading.credits, KnownCreditBalance)
            if reading.at > previous.at:
                used += max(previous.credits.value - reading.credits.value, 0.0)
                coverage_start = min(coverage_start, max(previous.at, window.start))
                coverage_end = max(coverage_end, reading.at)
                measured = True
    if samples == 0:
        return NoCreditHistory()
    if not measured or coverage_end <= coverage_start:
        return InsufficientCreditHistory()
    days = (coverage_end - coverage_start).total_seconds() / 86_400
    return KnownCreditRate(used / days)


def known_rate(numerator: float, denominator: float) -> Rate:
    return KnownRate(numerator / denominator) if denominator > 0 else UnavailableRate()


def format_number(value: float) -> str:
    return f"{value:,.1f}"


def format_percent(value: Rate) -> str:
    return f"{value.value:.1%}" if isinstance(value, KnownRate) else "—"


def format_rate(value: Rate) -> str:
    return f"{value.value:.2f}" if isinstance(value, KnownRate) else "—"


def format_credits(value: CreditRate) -> str:
    if isinstance(value, KnownCreditRate):
        return format_number(value.per_day)
    return "no credit history" if isinstance(value, NoCreditHistory) else "insufficient credit history"


def format_switches(value: SwitchRate) -> str:
    return f"{value.per_week:.1f}" if isinstance(value, KnownSwitchRate) else "—"


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--db", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    _ = mode.add_argument("--before", nargs=2, metavar=("START", "END"))
    _ = mode.add_argument("--daily", action="store_true")
    _ = parser.add_argument("--after", nargs=2, metavar=("START", "END"))
    _ = parser.add_argument("--t-live")
    return parser


def parse_arguments(argv: Sequence[str]) -> CommandLine:
    return argument_parser().parse_args(argv, namespace=CommandLine())


def requested_windows(args: CommandLine, now: datetime) -> WindowPair:
    if args.daily:
        if args.after is not None:
            raise ValueError("--after cannot be used with --daily")
        live_at = parse_boundary(args.t_live) if args.t_live is not None else DEFAULT_T_LIVE
        return WindowPair(
            Window(live_at - timedelta(days=7), live_at),
            Window(live_at, now.astimezone(timezone.utc)),
        )
    if args.before is None or args.after is None:
        raise ValueError("--before and --after must be given together")
    if args.t_live is not None:
        raise ValueError("--t-live is used only with --daily")
    return WindowPair(window_from(args.before), window_from(args.after))


def utc_minute(at: datetime) -> str:
    return f"{at.astimezone(timezone.utc):%Y-%m-%d %H:%M}Z"


class WindowName(Enum):
    BEFORE = "Before"
    AFTER = "After"


@dataclass(frozen=True)
class WholeWindowLabel:
    pass


@dataclass(frozen=True)
class PieceStartLabel:
    at: datetime


@dataclass(frozen=True)
class WindowPiece:
    name: WindowName
    window: Window
    label: WholeWindowLabel | PieceStartLabel

    def text(self) -> str:
        if isinstance(self.label, PieceStartLabel):
            return f"{self.name.value}, from {utc_minute(self.label.at)}"
        return self.name.value


@dataclass(frozen=True)
class InForceFromBeginning:
    pass


@dataclass(frozen=True)
class DatedWeightStart:
    at: datetime


@dataclass(frozen=True)
class WeightVersion:
    model: str
    start: InForceFromBeginning | DatedWeightStart
    cached: float
    output: float
    cache_write_5m: float
    cache_write_1h: float

    def text(self) -> str:
        dated = f"@{self.start.at:%Y-%m-%dT%H:%M}Z" if isinstance(self.start, DatedWeightStart) else ""
        return f"{self.model}{dated}: 1 / {self.cached:g} / {self.output:g}"

    def order(self) -> tuple[str, datetime]:
        return self.model, (self.start.at if isinstance(self.start, DatedWeightStart)
                            else datetime.min.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class ReadInFullAt:
    at: datetime


@dataclass(frozen=True)
class NotReadInFullSince:
    at: datetime


@dataclass(frozen=True)
class NeverReadInFull:
    pass


SourceFreshness = ReadInFullAt | NotReadInFullSince | NeverReadInFull


@dataclass(frozen=True)
class StoredWindowUsage:
    threads_opened: int
    active_threads: int
    weighted: float
    edited_weighted: float
    before_edit_weighted: float
    compactions: int
    checkpoints: int
    versions: frozenset[WeightVersion]
    turns: int
    unweighted: dict[str, int]


def store_readings(db: sqlite3.Connection) -> tuple[AccountReading, ...]:
    rows = cast(Iterable[sqlite3.Row], db.execute(
        "SELECT account, at, credits_state, credits FROM account_readings ORDER BY at, account"))
    readings: list[AccountReading] = []
    for row in rows:
        label = parse_account_label(cast(str, row["account"]).casefold())
        if not isinstance(label, NumberedAccountLabel) or label.tool != "codex":
            continue
        readings.append(AccountReading(label.label, datetime.fromisoformat(cast(str, row["at"])),
                                       credit_balance(cast(object, row["credits"])) if row["credits_state"] == "read"
                                       else UnknownCreditBalance()))
    return tuple(readings)


def mac_freshness(db: sqlite3.Connection) -> SourceFreshness:
    row = cast(sqlite3.Row | None, db.execute("SELECT state, last_complete_at FROM turn_sources "
                     + "WHERE machine = 'mac' AND family = 'codex'").fetchone())
    if row is None or not isinstance(row[1], str):
        return NeverReadInFull()
    at = datetime.fromisoformat(cast(str, row[1]))
    return ReadInFullAt(at) if row[0] == "ready" else NotReadInFullSince(at)


def window_pieces(db: sqlite3.Connection, name: WindowName, window: Window) -> tuple[WindowPiece, ...]:
    models = {cast(str, row[0]) for row in cast(Iterable[sqlite3.Row], db.execute(
        "SELECT DISTINCT model FROM turns WHERE family = 'codex' AND at >= ? AND at < ?",
        (window.start.astimezone(timezone.utc).isoformat(), window.end.astimezone(timezone.utc).isoformat())))}
    cuts = sorted({entry.effective_from for entry in weights.MODEL_WEIGHTS
                   if entry.model in models
                   and window.start < entry.effective_from < window.end})
    bounds = (window.start, *cuts, window.end)
    return tuple(WindowPiece(name, Window(start, end), PieceStartLabel(start) if cuts else WholeWindowLabel())
                 for start, end in zip(bounds, bounds[1:]))


def stored_window_usage(db: sqlite3.Connection, window: Window) -> StoredWindowUsage:
    bounds = (window.start.astimezone(timezone.utc).isoformat(), window.end.astimezone(timezone.utc).isoformat())
    sql = weights.sql_weights("t.model", "t.at")
    tokens = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
    totals = ", ".join(f"total(t.{column}) AS {column}" for column in tokens)
    query = f"""
        WITH weight_entries AS ({sql.entries}),
        first_edits AS MATERIALIZED (
            SELECT machine, parent_id, agent_id, min(at) AS at FROM edits WHERE family = 'codex'
            GROUP BY machine, parent_id, agent_id
        ), grouped AS (
            SELECT t.model, {sql.active_entry} AS weight_entry,
                   e.at IS NOT NULL AS edited, t.before_first_edit AS before_edit,
                   count(*) AS turns, {totals}
            FROM turns t LEFT JOIN first_edits e
              ON (e.machine, e.parent_id, e.agent_id) = (t.machine, t.parent_id, t.agent_id)
            WHERE t.family = 'codex' AND t.at >= ? AND t.at < ?
            GROUP BY t.model, weight_entry, edited, before_edit
        )
        SELECT g.model, g.weight_entry, g.edited, g.before_edit, g.turns,
               CASE WHEN w.weight_entry IS NULL THEN NULL ELSE g.input + g.cache_read * w.cached
                 + g.cache_write_5m * w.write_5m + g.cache_write_1h * w.write_1h + g.output * w.output END AS weighted
        FROM grouped g LEFT JOIN weight_entries w ON w.weight_entry = g.weight_entry
    """
    total = edited = before_edit = 0.0
    turns = 0
    unweighted: dict[str, int] = {}
    versions: set[WeightVersion] = set()
    for row in cast(Iterable[sqlite3.Row], db.execute(query, bounds)):
        count = cast(int, row["turns"])
        turns += count
        model = cast(str, row["model"])
        amount = cast(object, row["weighted"])
        if not isinstance(amount, (float, int)):
            unweighted[model] = unweighted.get(model, 0) + count
            continue
        total += amount
        if row["edited"]:
            edited += amount
            if row["before_edit"]:
                before_edit += amount
        entry = weights.MODEL_WEIGHTS[cast(int, row["weight_entry"])]
        start = (InForceFromBeginning() if entry.effective_from == datetime.min.replace(tzinfo=timezone.utc)
                 else DatedWeightStart(entry.effective_from))
        versions.add(WeightVersion(model, start, entry.cached, entry.output,
                                   entry.cache_write_5m, entry.cache_write_1h))
    active = " UNION ".join(
        f"SELECT machine, parent_id, agent_id FROM {table} WHERE family = 'codex' AND at >= ? AND at < ?"
        for table in ("openings", "turns", "edits", "compactions"))
    active_threads = cast(int, db.execute("SELECT count(*) FROM (" + active + ")", bounds * 4).fetchone()[0])

    def count_rows(table: str, codex: str) -> int:
        return cast(int, db.execute(f"SELECT count(*) FROM {table} WHERE at >= ? AND at < ?{codex}",
                                    bounds).fetchone()[0])

    return StoredWindowUsage(count_rows("openings", " AND family = 'codex'"), active_threads,
                             total, edited, before_edit, count_rows("compactions", " AND family = 'codex'"),
                             count_rows("checkpoints", ""), frozenset(versions), turns, unweighted)


def build_report(windows: WindowPair, db: sqlite3.Connection) -> str:
    readings = store_readings(db)
    history = store_switch_history(db)
    pieces = (*window_pieces(db, WindowName.BEFORE, windows.before),
              *window_pieces(db, WindowName.AFTER, windows.after))
    measured = tuple((piece, stored_window_usage(db, piece.window)) for piece in pieces)
    versions = set(version for _, usage in measured for version in usage.versions)
    lines = ["Weighting: " + ("; ".join(version.text() for version in sorted(versions, key=lambda v: v.order())) or "none"),
             "| Window | Threads opened | Weighted usage | Before first edit | Compactions (per thread) | Checkpointed phases | Account switches/week | Credits used/day |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    unweighted: dict[str, int] = {}
    for piece, usage in measured:
        lines.append(f"| {piece.text()} | {usage.threads_opened} | {format_number(usage.weighted)} | "
                     + f"{format_percent(known_rate(usage.before_edit_weighted, usage.edited_weighted))} | "
                     + f"{usage.compactions} ({format_rate(known_rate(usage.compactions, usage.active_threads))}) | "
                     + f"{usage.checkpoints} | {format_switches(switches_per_week(readings, piece.window, history))} | "
                     + f"{format_credits(credits_per_day(readings, piece.window))} |")
        for model, count in usage.unweighted.items():
            unweighted[model] = unweighted.get(model, 0) + count
    lines.append("Unweighted: " + ("; ".join(f"{model}: {count} {'turn' if count == 1 else 'turns'}" for model, count in sorted(unweighted.items())) or "none"))
    before_rows = [usage for piece, usage in measured if piece.name is WindowName.BEFORE]
    before = before_rows[0] if len(before_rows) == 1 else stored_window_usage(db, windows.before)
    after = measured[-1][1]
    before_rate = known_rate(before.weighted, before.checkpoints)
    after_rate = known_rate(after.weighted, after.checkpoints)
    before_text = format_number(before_rate.value) if isinstance(before_rate, KnownRate) else "unavailable"
    after_text = format_number(after_rate.value) if isinstance(after_rate, KnownRate) else "unavailable"
    change = "change unavailable"
    if isinstance(before_rate, KnownRate) and isinstance(after_rate, KnownRate) and before_rate.value:
        percent = (after_rate.value / before_rate.value - 1.0) * 100
        change = "unchanged" if abs(percent) < 0.05 else f"{'up' if percent > 0 else 'down'} {abs(percent):.1f}%"
    notes: list[str] = []
    if before.versions != after.versions:
        notes.append("different weights: compare roughly")
    before_missing, after_missing = sum(before.unweighted.values()), sum(after.unweighted.values())
    if before_missing or after_missing:
        notes.append(f"unweighted turns left out: before {before_missing} of {before.turns}, after {after_missing} of {after.turns}")
    freshness = mac_freshness(db)
    if isinstance(freshness, NotReadInFullSince):
        notes.append("Mac as of " + utc_minute(freshness.at))
    elif isinstance(freshness, NeverReadInFull):
        notes.append("Mac never read in full")
    note = " (" + "; ".join(notes) + ")" if notes else ""
    lines.extend(("", *usage_report.sources(db, ZoneInfo("UTC"))))
    lines.append(f"Weighted usage per checkpointed phase: before {before_text}; after {after_text}; {change}{note}.")
    return "\n".join(lines).rstrip()


def run(argv: Sequence[str]) -> int:
    args = parse_arguments(argv)
    try:
        windows = requested_windows(args, datetime.now(timezone.utc))
        path = collect.store_path() if isinstance(args.db, DefaultStore) else args.db
        with closing(usage_report.open_read_store(path)) as db:
            print(build_report(windows, db))
    except ValueError as error:
        print(f"worker_usage.py: {error}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print("worker_usage.py: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
