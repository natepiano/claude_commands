#!/usr/bin/env python3
"""Pace Codex's fast tier so the quota reaches each weekly refresh with its buffer left.

agent_notes.py calls `tick` every two minutes from the agent-sessions timer,
after it has written every Codex account's remaining usage, weekly reset and
limit resets. The registry tier `pace` (`/agent pace`) makes each codex launch
read the decision written here; under any other tier nothing reads it, and the
measuring carries on regardless.

Every tick re-plans from the live remaining usage, so any miss corrects itself:

1. Measure. Each rollout under ~/.codex/sessions adds its tokens to an hourly
   bucket under its thread's tier and model, and its quota readings add the rise
   in used percent. A least-squares fit over two weeks of hours that ran the
   dominant model gives the cost of its token at each tier; their ratio k is what
   fast costs over default. Models differ several-fold, so no hour mixing them
   enters the fit.
2. Forecast. Demand D is what each hour spent, deflated by its fast share to the
   default-tier spend, averaged at the busiest of the last day, three days and
   week, so a heavy day counts in full.
3. Budget. Each Codex account's next weekly reset is a checkpoint. Spend before
   one may use every account's remaining usage, every limit reset, and 100% for
   each account refreshed before it, less BUFFER held at it and at each earlier
   one (the leftover an account loses when it resets). The tightest checkpoint
   sets the affordable rate A, in percent per hour.
4. Decide. Running fast a share f of the time spends D·(1 + f·(k − 1)) an hour,
   so f = (A/D − 1)/(k − 1), held to [0, 1]. When default alone outspends A,
   f is 0 and nothing runs fast. Fast takes the first f of every half hour, so
   it is spread through the week rather than spent first.

A thread keeps the tier it launched with, so its tokens count under the tier
the pacer recorded for its start time unless the rollout names its own.
State lives in ~/.local/state/codex-pacer; `status` explains the current plan,
`backfill` rebuilds the measurements from older rollouts, and running
agent_notes.py by hand is one tick.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, TypedDict, cast

STATE_DIR = Path(os.environ.get("CODEX_PACER_STATE_DIR", Path.home() / ".local" / "state" / "codex-pacer"))
SESSIONS = Path.home() / ".codex" / "sessions"
AGENTS_CONF = Path.home() / ".claude" / "config" / "agents.conf"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

BUFFER = 5.0                 # percent left at every checkpoint
SLOT = timedelta(minutes=30)  # fast takes the first share of each slot
HORIZON = timedelta(days=8)   # checkpoints further out than a week are not planned
FIT_DAYS = 14
DEMAND_HOURS = (24, 72, 168)
SCAN_DAYS = 3                 # rollout directories a tick re-reads: a thread can outlive its day
K_PRIOR = 2.3                 # fit over Sep 22 - Oct 1, 2026, before the pacer measured its own
MIN_TIER_SHARE = 0.1          # each tier needs this share of the tokens for the fit to stand
PURE_MODEL = 0.95             # an hour enters the fit when the dominant model ran this share of it
MAX_STEP = 3.0                # a larger rise between readings is a switch of account, not spend
RESET_DROP = 5.0              # a larger fall within one window is a limit reset

# Relative token costs, in proportion to API prices: a cached input token is a
# tenth of a fresh one, an output token eight times.
CACHED_WEIGHT = 0.1
OUTPUT_WEIGHT = 8.0

STATE_FILE = STATE_DIR / "state.json"
PLAN_FILE = STATE_DIR / "plan.json"
TIER_FILE = STATE_DIR / "tier"  # read by _agents_pace_tier in agents_config.sh


class AgentNote(Protocol):
    tool: str

    def get(self, key: str) -> str | None: ...


class FileState(TypedDict):
    offset: int
    tier: str
    model: str


class Bucket(TypedDict):
    spent: float
    tokens: dict[str, float]  # "<tier> <model>" -> weighted tokens


class State(TypedDict):
    files: dict[str, FileState]
    buckets: dict[str, Bucket]
    windows: dict[str, float]
    history: list[list[str]]


class Checkpoint(TypedDict):
    at: str
    hours: float
    budget: float
    rate: float


class Plan(TypedDict):
    at: str
    tier: str
    registry_tier: str
    fast_share: float
    k: float
    k_hours: int
    model: str
    demand: float
    demand_windows: dict[str, float]
    affordable: float | None
    checkpoints: list[Checkpoint]


class Usage(TypedDict, total=False):
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int


class Primary(TypedDict, total=False):
    used_percent: float
    resets_at: int


class RateLimits(TypedDict, total=False):
    limit_id: str
    primary: Primary | None


class ThreadSettings(TypedDict, total=False):
    service_tier: str | None
    model: str


class Payload(TypedDict, total=False):
    type: str
    timestamp: str
    model: str
    usage: Usage
    rate_limits: RateLimits | None
    thread_settings: ThreadSettings


class Line(TypedDict, total=False):
    timestamp: str
    type: str
    payload: Payload


@dataclass(frozen=True)
class Account:
    remaining: float
    resets: datetime | None
    limit_resets: int


@dataclass(frozen=True)
class Costs:
    k: float      # what a fast token costs over a default one
    hours: int    # buckets the fit stood on; 0 means K_PRIOR
    model: str


def empty_state() -> State:
    return {"files": {}, "buckets": {}, "windows": {}, "history": []}


def load_state() -> State:
    try:
        return cast(State, json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return empty_state()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    _ = tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def hour_key(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def bucket(state: State, at: datetime) -> Bucket:
    return state["buckets"].setdefault(hour_key(at), {"spent": 0.0, "tokens": {}})


def fast_tokens(b: Bucket) -> float:
    return sum(v for key, v in b["tokens"].items() if key.startswith("fast "))


def weighted(usage: Usage) -> float:
    cached = usage.get("cached_input_tokens", 0)
    fresh = usage.get("input_tokens", 0) - cached
    return fresh + CACHED_WEIGHT * cached + OUTPUT_WEIGHT * usage.get("output_tokens", 0)


def tier_at(history: list[list[str]], at: datetime) -> str:
    """The launch tier in force at `at`: the last recorded change at or before it."""
    tier = "default"
    for stamp, value in history:
        if parse_time(stamp) > at:
            break
        tier = value
    return tier


def rollout_files(now: datetime, days: int) -> Iterator[Path]:
    local = now.astimezone()
    for back in range(days, -1, -1):
        day = local - timedelta(days=back)
        yield from sorted((SESSIONS / day.strftime("%Y/%m/%d")).glob("rollout-*.jsonl"))


def ingest(state: State, files: Iterator[Path]) -> None:
    """Read what each rollout appended since the last pass into the hourly buckets."""
    readings: list[tuple[datetime, float, str]] = []
    for path in files:
        key = str(path)
        known = state["files"].get(key)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = known["offset"] if known and known["offset"] <= size else 0
        if known and offset == size:
            continue
        with path.open("rb") as handle:
            _ = handle.seek(offset)
            data = handle.read()
        end = data.rfind(b"\n") + 1
        entry: FileState = known or {"offset": 0, "tier": "", "model": ""}
        entry["offset"] = offset + end
        state["files"][key] = entry
        for raw in data[:end].split(b"\n"):
            try:
                read_line(state, entry, raw, readings)
            except (ValueError, KeyError, TypeError):
                continue  # a line codex wrote in a shape this does not know
    readings.sort()
    for at, used, window in readings:
        high = state["windows"].get(window)
        if high is not None and high < used <= high + MAX_STEP:
            bucket(state, at)["spent"] += used - high
        if high is None or used > high or used < high - RESET_DROP:
            state["windows"][window] = used


def read_line(state: State, entry: FileState, raw: bytes,
              readings: list[tuple[datetime, float, str]]) -> None:
    if b'"token_usage_record"' in raw:
        line = cast(Line, json.loads(raw))
        tokens = bucket(state, parse_time(line.get("timestamp", "")))["tokens"]
        key = f"{tier_name(entry['tier'])} {entry['model'] or '?'}"
        tokens[key] = tokens.get(key, 0.0) + weighted(line.get("payload", {}).get("usage", {}))
    elif b'"token_count"' in raw:
        line = cast(Line, json.loads(raw))
        limits = line.get("payload", {}).get("rate_limits") or {}
        primary = limits.get("primary") or {}
        if limits.get("limit_id") == "codex" and "used_percent" in primary:
            window = str(round(primary.get("resets_at", 0) / 3600))
            readings.append((parse_time(line.get("timestamp", "")), primary["used_percent"], window))
    elif not entry["tier"] and b'"session_meta"' in raw[:200]:
        line = cast(Line, json.loads(raw))
        entry["tier"] = tier_at(state["history"], parse_time(line.get("payload", {}).get("timestamp", "")))
    elif not entry["model"] and b'"turn_context"' in raw[:200]:
        line = cast(Line, json.loads(raw))
        entry["model"] = line.get("payload", {}).get("model", "")
    elif b'"thread_settings_applied"' in raw:
        line = cast(Line, json.loads(raw))
        settings = line.get("payload", {}).get("thread_settings", {})
        named = settings.get("service_tier")
        if named:
            entry["tier"] = "fast" if named == "priority" else "default"
        entry["model"] = settings.get("model") or entry["model"]


def tier_name(tier: str) -> str:
    """Bucket key: flex runs no faster than default, so it is measured with it."""
    return "fast" if tier == "fast" else "default"


def fit(state: State, now: datetime) -> Costs:
    """Least squares of spend on each tier's tokens, hour by hour, without intercept."""
    since = hour_key(now - timedelta(days=FIT_DAYS))
    recent = [b for key, b in state["buckets"].items() if key >= since]
    by_model: dict[str, float] = {}
    for b in recent:
        for key, value in b["tokens"].items():
            model = key.split(" ", 1)[1]
            by_model[model] = by_model.get(model, 0.0) + value
    model = max(by_model, key=lambda m: by_model[m], default="?")
    pairs: list[tuple[float, float, float]] = []
    for b in recent:
        default = b["tokens"].get(f"default {model}", 0.0)
        fast = b["tokens"].get(f"fast {model}", 0.0)
        total = sum(b["tokens"].values())
        if total > 0 and default + fast >= PURE_MODEL * total:
            pairs.append((default, fast, b["spent"]))
    dd = sum(d * d for d, _, _ in pairs)
    df = sum(d * f for d, f, _ in pairs)
    ff = sum(f * f for _, f, _ in pairs)
    dy = sum(d * y for d, _, y in pairs)
    fy = sum(f * y for _, f, y in pairs)
    total_default = sum(d for d, _, _ in pairs)
    total_fast = sum(f for _, f, _ in pairs)
    tokens = total_default + total_fast
    det = dd * ff - df * df
    if tokens > 0 and min(total_default, total_fast) / tokens >= MIN_TIER_SHARE and det > 0:
        default = (dy * ff - df * fy) / det
        fast = (dd * fy - df * dy) / det
        if default > 0 and 1 <= fast / default <= 5:
            return Costs(fast / default, len(pairs), model)
    return Costs(K_PRIOR, 0, model)


def demand(state: State, k: float, now: datetime) -> dict[str, float]:
    """Default-tier spend per hour over each window: each hour's spend, less its fast premium."""
    windows: dict[str, float] = {}
    for hours in DEMAND_HOURS:
        keys = {hour_key(now - timedelta(hours=back)) for back in range(hours)}
        spent = 0.0
        for key in keys & state["buckets"].keys():
            b = state["buckets"][key]
            total = sum(b["tokens"].values())
            share = fast_tokens(b) / total if total > 0 else 0.0
            spent += b["spent"] / (1 + share * (k - 1))
        windows[f"{hours}h"] = spent / hours
    return windows


def accounts(notes: Sequence[AgentNote], now: datetime) -> list[Account]:
    found: list[Account] = []
    local_now = now.astimezone().replace(tzinfo=None)
    for note in notes:
        if note.tool != "codex":
            continue
        resets = parse_local(note.get("resets"))
        remaining = parse_float(note.get("weekly_remaining_usage"))
        if resets is None or resets <= local_now:
            # A weekly window that has closed has refreshed; one that opens on first
            # use starts full.
            resets, remaining = None, 100.0
        count = int(parse_float(note.get("limit_reset_count")) or 0)
        expiry = note.get("limit_reset")
        if count and expiry and expiry != "null" and parse_time(expiry) <= now:
            count -= 1  # the note keeps only the earliest expiry; drop the one that lapsed
        found.append(Account(remaining or 0.0, resets, count))
    return found


def parse_local(value: str | None) -> datetime | None:
    if not value or value == "null":
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def parse_float(value: str | None) -> float | None:
    try:
        return float(value) if value and value != "null" else None
    except ValueError:
        return None


def checkpoints(found: list[Account], now: datetime) -> list[Checkpoint]:
    local_now = now.astimezone().replace(tzinfo=None)
    available = sum(a.remaining for a in found) + 100 * sum(a.limit_resets for a in found)
    resets = sorted(a.resets for a in found if a.resets is not None and a.resets - local_now <= HORIZON)
    points: list[Checkpoint] = []
    for index, at in enumerate(resets):
        budget = available + 100 * index - BUFFER * (index + 1)
        hours = max((at - local_now).total_seconds() / 3600, 1 / 60)
        points.append({"at": at.strftime("%Y-%m-%dT%H:%M"), "hours": hours, "budget": budget, "rate": budget / hours})
    return points


def fast_share(affordable: float | None, demand_rate: float, k: float) -> float:
    """The share of time fast can run so the spend stays at `affordable` per hour."""
    if affordable is None or demand_rate <= 0 or k <= 1:
        return 0.0
    return min(1.0, max(0.0, (affordable / demand_rate - 1) / (k - 1)))


def slot_tier(share: float, now: datetime) -> str:
    into = (now - now.replace(minute=0, second=0, microsecond=0)) % SLOT
    return "fast" if into < SLOT * share else "default"


def make_plan(state: State, notes: Sequence[AgentNote], registry: str, now: datetime) -> Plan:
    costs = fit(state, now)
    k = costs.k
    windows = demand(state, k, now)
    rate = max(windows.values())
    points = checkpoints(accounts(notes, now), now)
    affordable = min((p["rate"] for p in points), default=None)
    share = fast_share(affordable, rate, k)
    return {
        "at": now.isoformat(timespec="seconds"),
        "tier": slot_tier(share, now),
        "registry_tier": registry,
        "fast_share": share,
        "k": k,
        "k_hours": costs.hours,
        "model": costs.model,
        "demand": rate,
        "demand_windows": windows,
        "affordable": affordable,
        "checkpoints": points,
    }


def registry_tier() -> str:
    """The delegate rows' stored tier: what the bulk of codex launches carry."""
    section = ""
    try:
        lines = AGENTS_CONF.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        text = line.split("#", 1)[0].strip()
        if text.startswith("["):
            section = text.strip("[]")
        elif section == "delegate.options" and text.startswith("codex_service_tier="):
            return text.split("=", 1)[1].strip()
    return ""


def config_tier() -> str:
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^service_tier\s*=\s*"?([^"\n]*)"?\s*$', text.split("\n[", 1)[0], re.MULTILINE)
    return match.group(1) if match else ""


def launch_tier(registry: str, decided: str) -> str:
    if registry == "pace":
        return decided
    return tier_name(registry or config_tier())


def record(state: State, tier: str, now: datetime) -> None:
    if not state["history"] or state["history"][-1][1] != tier:
        state["history"].append([now.isoformat(timespec="seconds"), tier])


def prune(state: State, now: datetime) -> None:
    oldest = hour_key(now - timedelta(days=FIT_DAYS + 1))
    state["buckets"] = {k: b for k, b in state["buckets"].items() if k >= oldest}
    keep = {str(p) for p in rollout_files(now, SCAN_DAYS)}
    state["files"] = {k: f for k, f in state["files"].items() if k in keep}
    hour = now.timestamp() / 3600
    state["windows"] = {k: v for k, v in state["windows"].items() if float(k) > hour - 24}
    cutoff = now - timedelta(days=FIT_DAYS + 1)
    old = [h for h in state["history"] if parse_time(h[0]) < cutoff]
    state["history"] = old[-1:] + [h for h in state["history"] if parse_time(h[0]) >= cutoff]


def tick(notes: Sequence[AgentNote], now: datetime | None = None) -> Plan:
    now = now or datetime.now(timezone.utc)
    state = load_state()
    ingest(state, rollout_files(now, SCAN_DAYS))
    registry = registry_tier()
    plan = make_plan(state, notes, registry, now)
    record(state, launch_tier(registry, plan["tier"]), now)
    prune(state, now)
    write_atomic(STATE_FILE, json.dumps(state))
    write_atomic(PLAN_FILE, json.dumps(plan, indent=2) + "\n")
    write_atomic(TIER_FILE, plan["tier"] + "\n")
    return plan


def backfill(days: int, fast_from: datetime | None, fast_until: datetime | None) -> State:
    """Rebuild the measurements from older rollouts, given when fast was in force."""
    now = datetime.now(timezone.utc)
    state = empty_state()
    if fast_from:
        state["history"].append([fast_from.isoformat(timespec="seconds"), "fast"])
        if fast_until:
            state["history"].append([fast_until.isoformat(timespec="seconds"), "default"])
    ingest(state, rollout_files(now, days))
    record(state, launch_tier(registry_tier(), "default"), now)
    prune(state, now)
    write_atomic(STATE_FILE, json.dumps(state))
    return state


def describe(plan: Plan) -> list[str]:
    def when(value: str) -> str:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M").strftime("%a %b %-d %H:%M")

    in_charge = plan["registry_tier"] == "pace"
    lines = [
        f"Tier now: {plan['tier']}" + ("" if in_charge else
            f" (not in force: the registry tier is {plan['registry_tier'] or 'inherit'}; `/agent pace` hands it over)"),
        f"Fast share: {plan['fast_share']:.0%} of each half hour",
        f"Fast costs {plan['k']:.2f}x default per {plan['model']} token: "
        + (f"fit over {plan['k_hours']} hours" if plan["k_hours"] else "prior from Sep 22 - Oct 1; not enough of both tiers to fit"),
        "Demand at default: {:.2f}%/h (busiest of {})".format(
            plan["demand"], ", ".join(f"{k} {v:.2f}" for k, v in plan["demand_windows"].items())),
    ]
    points = plan["checkpoints"]
    if not points:
        return [*lines, "No Codex reset known within a week: running default."]
    for point in points:
        lines.append(f"  by {when(point['at'])} ({point['hours']:.0f} h): {point['budget']:.0f}% after buffers"
                     + f" -> {point['rate']:.2f}%/h")
    tight = min(points, key=lambda p: p["rate"])
    spend = plan["demand"] * (1 + plan["fast_share"] * (plan["k"] - 1))
    lines.append(f"Affordable {tight['rate']:.2f}%/h; this share spends {spend:.2f}%/h.")
    if plan["demand"] > tight["rate"] > 0:
        short = tight["hours"] - tight["budget"] / plan["demand"]
        lines.append(f"Even at default this runs out about {short:.0f} h before {when(tight['at'])}.")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    _ = commands.add_parser("status", help="explain the current plan")
    rebuild = commands.add_parser("backfill", help="rebuild measurements from older rollouts")
    _ = rebuild.add_argument("--days", type=int, default=FIT_DAYS)
    _ = rebuild.add_argument("--fast-from", type=datetime.fromisoformat)
    _ = rebuild.add_argument("--fast-until", type=datetime.fromisoformat)
    args = parser.parse_args()
    command = cast(str, args.command)
    if command == "backfill":
        state = backfill(cast(int, args.days), cast(datetime | None, args.fast_from), cast(datetime | None, args.fast_until))
        print(f"rebuilt {len(state['buckets'])} hours from {len(state['files'])} recent rollouts")
        return
    try:
        plan = cast(Plan, json.loads(PLAN_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        sys.exit("no plan yet: the timer has not run the pacer (agent_notes.py runs one tick)")
    for line in describe(plan):
        print(line)


if __name__ == "__main__":
    main()
