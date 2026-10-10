#!/usr/bin/env python3
"""Compare Codex usage per checkpointed phase before and after long-lived seats."""

from __future__ import annotations

import argparse
import json
import math
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, TypedDict, cast

CACHED_WEIGHT = 0.1
OUTPUT_WEIGHT = 8.0
PRICE_BASIS_DATE = "2026-10-10"
SEAT_MODEL = "gpt-5.6-sol"
DEFAULT_T_LIVE = datetime(2026, 10, 10, 18, 17, tzinfo=timezone.utc)
MAC_HOST = "natemccoy@mac"


class TokenUsageJson(TypedDict, total=False):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


class TokenInfoJson(TypedDict, total=False):
    last_token_usage: TokenUsageJson


class RolloutItemJson(TypedDict, total=False):
    type: str


class RolloutPayloadJson(TypedDict, total=False):
    type: str
    name: str
    timestamp: str
    input: object
    info: TokenInfoJson
    item: RolloutItemJson


class RolloutRecordJson(TypedDict, total=False):
    timestamp: str
    type: str
    payload: RolloutPayloadJson


class ReadingJson(TypedDict, total=False):
    account: str
    at: str
    remaining: float
    credits: float | int | str


@dataclass(frozen=True)
class TokenUsage:
    uncached_input: int = 0
    cached_input: int = 0
    output: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.uncached_input + other.uncached_input,
            self.cached_input + other.cached_input,
            self.output + other.output,
        )

    def weighted(self) -> float:
        return self.uncached_input + CACHED_WEIGHT * self.cached_input + OUTPUT_WEIGHT * self.output


@dataclass(frozen=True)
class KnownThreadOpening:
    at: datetime


@dataclass(frozen=True)
class UnknownThreadOpening:
    pass


ThreadOpening = KnownThreadOpening | UnknownThreadOpening


@dataclass(frozen=True)
class UsageSample:
    at: datetime
    usage: TokenUsage
    before_first_edit: bool


@dataclass(frozen=True)
class ThreadRollout:
    opening: ThreadOpening
    usage: tuple[UsageSample, ...]
    compactions: tuple[datetime, ...]
    edits: tuple[datetime, ...]
    ever_edited: bool


@dataclass
class ThreadRolloutBuilder:
    opening: ThreadOpening = field(default_factory=UnknownThreadOpening)
    usage: list[UsageSample] = field(default_factory=list)
    compactions: list[datetime] = field(default_factory=list)
    edits: list[datetime] = field(default_factory=list)
    edit_seen: bool = False
    record_seen: bool = False

    def consume(self, record: RolloutRecordJson) -> None:
        self.record_seen = True
        payload = payload_of(record)
        at = timestamp_of(record)
        if record.get("type") == "session_meta":
            opening = parse_timestamp(payload.get("timestamp"))
            if opening is None:
                opening = at
            if opening is not None:
                self.opening = KnownThreadOpening(opening)
        if is_edit(record):
            self.edit_seen = True
            if at is not None:
                self.edits.append(at)
        usage = token_usage(record)
        if usage is not None and at is not None:
            self.usage.append(UsageSample(at, usage, not self.edit_seen))
        if is_compaction(record) and at is not None:
            self.compactions.append(at)

    def finish(self) -> ThreadRollout:
        return ThreadRollout(
            self.opening,
            tuple(self.usage),
            tuple(self.compactions),
            tuple(self.edits),
            self.edit_seen,
        )


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("window boundaries must include a timezone")
        if self.start >= self.end:
            raise ValueError("window start must be before its end")

    def contains(self, at: datetime) -> bool:
        utc = at.astimezone(timezone.utc)
        return self.start <= utc < self.end

    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86_400


@dataclass(frozen=True)
class WindowPair:
    before: Window
    after: Window


@dataclass(frozen=True)
class WindowUsage:
    threads_opened: int
    active_threads: int
    total: TokenUsage
    edited_thread_total: TokenUsage
    before_first_edit: TokenUsage
    compactions: int


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
class WindowReport:
    label: str
    usage: WindowUsage
    before_edit_share: Rate
    compactions_per_thread: Rate
    checkpointed_phases: int
    switches_per_week: SwitchRate
    credits_per_day: CreditRate
    usage_per_phase: Rate


@dataclass(frozen=True)
class MacIncluded:
    threads: tuple[ThreadRollout, ...]


@dataclass(frozen=True)
class MacUnreachable:
    reason: str


MacRollouts = MacIncluded | MacUnreachable


@dataclass(frozen=True)
class ReportSources:
    local_sessions: Path
    live_readings: Path
    baseline_readings: Path
    claude_repo: Path
    production_docs: Path


class CommandLine(argparse.Namespace):
    before: list[str] | None
    after: list[str] | None
    daily: bool
    t_live: str | None

    def __init__(self) -> None:
        super().__init__()
        self.before = None
        self.after = None
        self.daily = False
        self.t_live = None


def payload_of(record: RolloutRecordJson) -> RolloutPayloadJson:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def decode_rollout(line: str) -> RolloutRecordJson | None:
    try:
        value = cast(object, json.loads(line))
    except (json.JSONDecodeError, UnicodeError):
        return None
    return cast(RolloutRecordJson, cast(object, value)) if isinstance(value, dict) else None


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_of(record: RolloutRecordJson) -> datetime | None:
    return parse_timestamp(record.get("timestamp"))


def token_usage(record: RolloutRecordJson) -> TokenUsage | None:
    payload = payload_of(record)
    if record.get("type") != "event_msg" or payload.get("type") != "token_count":
        return None
    info_value = payload.get("info")
    if not isinstance(info_value, dict):
        return None
    info = info_value
    last_value = info.get("last_token_usage")
    if not isinstance(last_value, dict):
        return None
    last = last_value
    input_tokens = last.get("input_tokens")
    cached_tokens = last.get("cached_input_tokens")
    output_tokens = last.get("output_tokens")
    if not all(isinstance(value, int) and not isinstance(value, bool)
               for value in (input_tokens, cached_tokens, output_tokens)):
        return None
    assert isinstance(input_tokens, int)
    assert isinstance(cached_tokens, int)
    assert isinstance(output_tokens, int)
    if min(input_tokens, cached_tokens, output_tokens) < 0 or cached_tokens > input_tokens:
        return None
    return TokenUsage(input_tokens - cached_tokens, cached_tokens, output_tokens)


def is_edit(record: RolloutRecordJson) -> bool:
    payload = payload_of(record)
    if record.get("type") != "response_item" or payload.get("type") != "custom_tool_call":
        return False
    if payload.get("name") == "apply_patch":
        return True
    tool_input = payload.get("input")
    if not isinstance(tool_input, str):
        return False
    try:
        decoded = cast(object, json.loads(tool_input))
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        fields = cast(dict[str, object], cast(object, decoded))
        if any(fields.get(key) == "apply_patch" for key in ("command", "name", "tool")):
            return True
    stripped = tool_input.lstrip()
    return stripped.startswith("apply_patch ") or re.search(r"\b(?:tools\.)?apply_patch\s*\(", tool_input) is not None


def is_compaction(record: RolloutRecordJson) -> bool:
    payload = payload_of(record)
    item_value = payload.get("item")
    item: RolloutItemJson = item_value if isinstance(item_value, dict) else {}
    return (
        record.get("type") == "compacted"
        or payload.get("type") in ("compacted", "contextCompaction")
        or item.get("type") == "contextCompaction"
    )


def parse_rollout(lines: Iterable[str]) -> ThreadRollout:
    builder = ThreadRolloutBuilder()
    for line in lines:
        record = decode_rollout(line)
        if record is not None:
            builder.consume(record)
    return builder.finish()


def parse_concatenated_rollouts(lines: Iterable[str]) -> tuple[ThreadRollout, ...]:
    threads: list[ThreadRollout] = []
    builder = ThreadRolloutBuilder()
    for line in lines:
        record = decode_rollout(line)
        if record is None:
            continue
        if record.get("type") == "session_meta" and builder.record_seen:
            threads.append(builder.finish())
            builder = ThreadRolloutBuilder()
        builder.consume(record)
    if builder.record_seen:
        threads.append(builder.finish())
    return tuple(threads)


def local_rollouts(root: Path, modified_since: datetime) -> tuple[ThreadRollout, ...]:
    threads: list[ThreadRollout] = []
    for path in sorted(root.glob("*/*/*/*.jsonl")):
        try:
            if path.stat().st_mtime < modified_since.timestamp():
                continue
            with path.open(encoding="utf-8", errors="replace") as lines:
                threads.append(parse_rollout(lines))
        except OSError:
            continue
    return tuple(threads)


def mac_failure_reason(stderr: str, returncode: int) -> str:
    lowered = stderr.lower()
    if "permission denied" in lowered:
        return "permission denied"
    if "connection refused" in lowered:
        return "connection refused"
    if "timed out" in lowered or "timeout" in lowered:
        return "connection timed out"
    if "could not resolve" in lowered or "no route to host" in lowered or "host is down" in lowered:
        return "host unavailable"
    return f"ssh exit {returncode}"


def remote_rollout_command(modified_since: datetime) -> str:
    script = "".join((
        "from pathlib import Path\n",
        "import shutil, sys\n",
        "cutoff = float(sys.argv[1])\n",
        "output = sys.stdout.buffer\n",
        "for path in Path.home().joinpath('.codex/sessions').glob('*/*/*/*.jsonl'):\n",
        " try:\n",
        "  if path.stat().st_mtime >= cutoff:\n",
        "   with path.open('rb') as source: shutil.copyfileobj(source, output)\n",
        "   output.write(b'\\n')\n",
        " except OSError: pass\n",
    ))
    return shlex.join(("python3", "-c", script, str(modified_since.timestamp())))


def mac_rollouts(modified_since: datetime) -> MacRollouts:
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", MAC_HOST,
        remote_rollout_command(modified_since),
    ]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return MacUnreachable("ssh unavailable")
        output: IO[str] | None = process.stdout
        if output is None:
            _ = process.wait()
            return MacUnreachable("ssh produced no stream")
        threads = parse_concatenated_rollouts(output)
        output.close()
        returncode = process.wait()
        if returncode == 0:
            return MacIncluded(threads)
        _ = errors.seek(0)
        return MacUnreachable(mac_failure_reason(errors.read(), returncode))


def window_usage(threads: Iterable[ThreadRollout], window: Window) -> WindowUsage:
    opened = 0
    active = 0
    total = TokenUsage()
    edited_total = TokenUsage()
    startup = TokenUsage()
    compactions = 0
    for thread in threads:
        thread_active = False
        if isinstance(thread.opening, KnownThreadOpening) and window.contains(thread.opening.at):
            opened += 1
            thread_active = True
        for sample in thread.usage:
            if not window.contains(sample.at):
                continue
            thread_active = True
            total += sample.usage
            if thread.ever_edited:
                edited_total += sample.usage
                if sample.before_first_edit:
                    startup += sample.usage
        for at in thread.compactions:
            if window.contains(at):
                thread_active = True
                compactions += 1
        if any(window.contains(at) for at in thread.edits):
            thread_active = True
        if thread_active:
            active += 1
    return WindowUsage(opened, active, total, edited_total, startup, compactions)


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


def git_text(repo: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def cell_value(value: str) -> str:
    quoted = re.search(r"`([^`]+)`", value)
    return quoted.group(1) if quoted else value.split(" — ", 1)[0].strip()


def production_paths(path: Path) -> tuple[Path, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ()
    paths: list[Path] = []
    for line in lines:
        if line.startswith("- **Repository:** "):
            value = cell_value(line.removeprefix("- **Repository:** "))
            if value:
                paths.append(Path(value).expanduser())
    try:
        units = lines.index("## Units")
    except ValueError:
        return tuple(paths)
    headings: list[str] = []
    for line in lines[units + 1:]:
        if not line.startswith("|"):
            if headings:
                break
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not headings:
            headings = cells
            continue
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        if "Worktree" not in headings:
            break
        values = dict(zip(headings, cells, strict=False))
        worktree = cell_value(values.get("Worktree", ""))
        if worktree:
            paths.append(Path(worktree).expanduser())
    return tuple(paths)


def repositories(claude_repo: Path, production_docs: Path) -> tuple[Path, ...]:
    candidates = [claude_repo]
    candidates.extend(
        path
        for document in sorted(production_docs.glob("*-production.md"))
        for path in production_paths(document)
    )
    unique: dict[str, Path] = {}
    for candidate in candidates:
        common_text = git_text(candidate, "rev-parse", "--git-common-dir")
        if common_text is None:
            continue
        raw = Path(common_text.strip())
        common = (raw if raw.is_absolute() else candidate / raw).resolve()
        _ = unique.setdefault(str(common), candidate)
    return tuple(unique.values())


def checkpoint_count(repos: Iterable[Path], window: Window) -> int:
    count = 0
    for repo in repos:
        output = git_text(repo, "log", "--all", "--format=%ct%x00%s")
        if output is None:
            continue
        for line in output.splitlines():
            seconds, separator, subject = line.partition("\x00")
            if not separator or not subject.startswith("checkpoint("):
                continue
            try:
                at = datetime.fromtimestamp(int(seconds), timezone.utc)
            except (ValueError, OverflowError):
                continue
            if window.contains(at):
                count += 1
    return count


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


def reading_records(path: Path) -> tuple[AccountReading, ...]:
    records: list[AccountReading] = []
    try:
        lines = path.open(encoding="utf-8")
    except OSError:
        return ()
    with lines:
        for line in lines:
            try:
                value = cast(object, json.loads(line))
            except (json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(value, dict):
                continue
            record = cast(ReadingJson, cast(object, value))
            account = record.get("account")
            at = parse_timestamp(record.get("at"))
            if not isinstance(account, str) or not account.lower().startswith("codex ") or at is None:
                continue
            records.append(AccountReading(account, at, credit_balance(record.get("credits"))))
    return tuple(records)


def merged_readings(live: Iterable[AccountReading], baseline: Iterable[AccountReading]) -> tuple[AccountReading, ...]:
    by_key = {(reading.account, reading.at): reading for reading in baseline}
    for reading in live:
        by_key[(reading.account, reading.at)] = reading
    return tuple(sorted(by_key.values(), key=lambda reading: (reading.at, reading.account)))


def switches_per_week(readings: Iterable[AccountReading], window: Window) -> SwitchRate:
    ordered = sorted((reading for reading in readings if reading.at < window.end), key=lambda item: item.at)
    prior = [reading for reading in ordered if reading.at < window.start]
    within = [reading for reading in ordered if reading.at >= window.start]
    if not within:
        return SwitchHistoryUnavailable()
    coverage_start = window.start if prior else within[0].at
    coverage_end = within[-1].at
    if coverage_end <= coverage_start:
        return SwitchHistoryUnavailable()
    previous_account = prior[-1].account if prior else within[0].account
    observations = within if prior else within[1:]
    switches = 0
    for reading in observations:
        if reading.account != previous_account:
            switches += 1
        previous_account = reading.account
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


def make_window_report(
    label: str,
    usage: WindowUsage,
    checkpoints: int,
    readings: Iterable[AccountReading],
    window: Window,
) -> WindowReport:
    return WindowReport(
        label,
        usage,
        known_rate(usage.before_first_edit.weighted(), usage.edited_thread_total.weighted()),
        known_rate(float(usage.compactions), float(usage.active_threads)),
        checkpoints,
        switches_per_week(readings, window),
        credits_per_day(readings, window),
        known_rate(usage.total.weighted(), float(checkpoints)),
    )


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


def render_report(before: WindowReport, after: WindowReport, mac: MacRollouts) -> str:
    lines: list[str] = []
    if isinstance(mac, MacUnreachable):
        lines.append(f"Mac: unreachable ({mac.reason})")
    lines.extend((
        f"Weighting: {SEAT_MODEL}, OpenAI published prices read {PRICE_BASIS_DATE}; "
        + "uncached input 1 · cached input 0.1 · output 8.",
        "| Window | Threads opened | Weighted usage | Before first edit | Compactions (per thread) | Checkpointed phases | Account switches/week | Credits used/day |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ))
    for report in (before, after):
        lines.append("".join((
            f"| {report.label} | {report.usage.threads_opened} | ",
            f"{format_number(report.usage.total.weighted())} | {format_percent(report.before_edit_share)} | ",
            f"{report.usage.compactions} ({format_rate(report.compactions_per_thread)}) | ",
            f"{report.checkpointed_phases} | ",
            f"{format_switches(report.switches_per_week)} | {format_credits(report.credits_per_day)} |",
        )))
    if isinstance(mac, MacUnreachable):
        lines.append("Weighted usage per checkpointed phase: unavailable because Mac data is unreachable.")
        return "\n".join(lines)
    if isinstance(before.usage_per_phase, KnownRate) and isinstance(after.usage_per_phase, KnownRate):
        if before.usage_per_phase.value == 0:
            change = "change unavailable"
        else:
            percent = (after.usage_per_phase.value / before.usage_per_phase.value - 1.0) * 100
            change = "unchanged" if abs(percent) < 0.05 else f"{'up' if percent > 0 else 'down'} {abs(percent):.1f}%"
        lines.append("".join((
            "Weighted usage per checkpointed phase: ",
            f"before {format_number(before.usage_per_phase.value)}; ",
            f"after {format_number(after.usage_per_phase.value)}; {change}.",
        )))
    else:
        before_value = format_number(before.usage_per_phase.value) if isinstance(before.usage_per_phase, KnownRate) else "unavailable"
        after_value = format_number(after.usage_per_phase.value) if isinstance(after.usage_per_phase, KnownRate) else "unavailable"
        lines.append(
            f"Weighted usage per checkpointed phase: before {before_value}; "
            + f"after {after_value}; change unavailable."
        )
    return "\n".join(lines)


def default_sources() -> ReportSources:
    home = Path.home()
    claude = home / ".claude"
    return ReportSources(
        home / ".codex" / "sessions",
        home / ".local" / "state" / "agent-notes" / "readings.jsonl",
        home / ".local" / "state" / "usage-model" / "seats-baseline" / "readings-2026-10-10.jsonl",
        claude,
        claude / "docs" / "plans",
    )


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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


def build_report(windows: WindowPair, sources: ReportSources, mac: MacRollouts) -> str:
    modified_since = min(windows.before.start, windows.after.start)
    local = local_rollouts(sources.local_sessions, modified_since)
    threads = local + (mac.threads if isinstance(mac, MacIncluded) else ())
    readings = merged_readings(
        reading_records(sources.live_readings),
        reading_records(sources.baseline_readings),
    )
    repos = repositories(sources.claude_repo, sources.production_docs)
    before = make_window_report(
        "Before",
        window_usage(threads, windows.before),
        checkpoint_count(repos, windows.before),
        readings,
        windows.before,
    )
    after = make_window_report(
        "After",
        window_usage(threads, windows.after),
        checkpoint_count(repos, windows.after),
        readings,
        windows.after,
    )
    return render_report(before, after, mac)


def run(argv: Sequence[str]) -> int:
    args = parse_arguments(argv)
    sources = default_sources()
    try:
        windows = requested_windows(args, datetime.now(timezone.utc))
    except ValueError as error:
        print(f"seat_usage.py: {error}", file=sys.stderr)
        return 2
    modified_since = min(windows.before.start, windows.after.start)
    mac = mac_rollouts(modified_since)
    print(build_report(windows, sources, mac))
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
