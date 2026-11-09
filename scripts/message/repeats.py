#!/usr/bin/env python3
"""Rank machine-message repeats or replay a log without delivering or writing state.

repeats.py [--here] [--day YYYY-MM-DD] [--replay] [--log PATH]
The default report joins local and peer summaries, not their private message bodies.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import json
import heapq
import os
import shlex
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.message import repeat_guard
from scripts.shutdown import remote
from scripts.lib.user_zone import user_zone  # noqa: E402


@dataclass
class SenderTotals:
    count: int = 0
    bytes: int = 0
    receivers: set[str] = field(default_factory=set)
    first: str = ""
    last: str = ""
    held: int = 0
    distinct_held: int = 0
    protected_held: int = 0
    watchdog_held: int = 0
    unlabelled: int = 0
    unknown_human: int = 0


def _records_file(path: Path, day: str) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = cast(object, json.loads(line))
                if isinstance(record, dict):
                    values = cast(dict[str, object], record)
                    at = datetime.fromisoformat(str(values.get("time", "")))
                    if at.astimezone(user_zone()).date().isoformat() == day:
                        yield values
            except (ValueError, TypeError):
                continue


def records(path: Path, day: str) -> Iterator[dict[str, object]]:
    streams = [_records_file(path, day)] if path.exists() else []
    direct = path.with_name("transports.jsonl")
    if path.name == "log.jsonl" and direct.exists():
        streams.append(_records_file(direct, day))
    yield from heapq.merge(*streams, key=lambda row: str(row["time"]))


def replay(rows: Iterable[dict[str, object]], limits: repeat_guard.Limits, *, simulate: bool = True) -> dict[str, SenderTotals]:
    """Pure replay: exactly the live transition, with no state files or send calls."""
    states: dict[str, repeat_guard.SenderState] = {}
    totals: dict[str, SenderTotals] = {}
    for row in rows:
        sender, receiver = str(row.get("from", "")), str(row.get("to", ""))
        text = str(row.get("text", row.get("summary", "")))
        at_text = str(row["time"])
        at = datetime.fromisoformat(at_text).timestamp()
        protected = sender.casefold() == "user" or row.get("human") is True or receiver == "user"
        attempt = repeat_guard.Attempt(sender, receiver, text, str(row.get("machine", "")), at, protected)
        state = states.setdefault(sender, repeat_guard.SenderState(sender, attempt.host, at))
        decision = (repeat_guard.transition(state, attempt, limits) if simulate else
                    repeat_guard.Decision(row.get("outcome") == "held"))
        item = totals.setdefault(sender, SenderTotals(first=at_text))
        item.count += 1
        item.bytes += len(text.encode())
        item.receivers.add(receiver)
        item.last = at_text
        item.held += int(decision.held)
        item.watchdog_held += int(decision.held and ("watchdog" in sender.casefold() or text.startswith("watchdog:")))
        # Labels come from a caller/fixture, never from the normalizer under test.
        item.distinct_held += int(decision.held and row.get("safety") == "distinct")
        item.unlabelled += int("safety" not in row)
        item.unknown_human += int("human" not in row and not protected)
        item.protected_held += int(decision.held and protected)
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--here", action="store_true")
    _ = parser.add_argument("--day", default=datetime.now(user_zone()).date().isoformat())
    _ = parser.add_argument("--log", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "message/log.jsonl")
    _ = parser.add_argument("--replay", action="store_true")
    _ = parser.add_argument("--snapshot", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    limits = repeat_guard.configuration()
    if isinstance(limits, repeat_guard.GuardUnavailable):
        return 2
    path = cast(Path, args.log)
    day = cast(str, args.day)
    here, replaying, snapshot = cast(bool, args.here), cast(bool, args.replay), cast(bool, args.snapshot)
    totals = replay(records(path, day), limits, simulate=replaying)
    if snapshot:
        print(json.dumps({sender: {**asdict(item), "receivers": sorted(item.receivers)} for sender, item in totals.items()}))
        return 0
    if not here and not replaying:
        host = remote.other_machine()
        command = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/repeats.py"'
                   + " --here --snapshot --day " + shlex.quote(day))
        try:
            result = subprocess.run(["ssh", "-o", "BatchMode=yes", host, command],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=20, check=False)
            if result.returncode != 0:
                raise ValueError(result.stderr.strip() or f"exit {result.returncode}")
            values = cast(dict[str, dict[str, object]], json.loads(result.stdout))
            for sender, raw in values.items():
                totals[f"{host}:{sender}"] = SenderTotals(int(cast(int, raw["count"])), int(cast(int, raw["bytes"])),
                    set(cast(list[str], raw["receivers"])), str(raw["first"]), str(raw["last"]), int(cast(int, raw["held"])))
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
            print(f"repeats: {host}: not read: {error}", file=sys.stderr)
    print("sender                           count    bytes sessions   held  first / last")
    for sender, item in sorted(totals.items(), key=lambda pair: pair[1].held, reverse=True)[:12]:
        print(f"{sender[:32]:32} {item.count:6} {item.bytes:8} {len(item.receivers):8} {item.held:6}  {item.first[11:19]} / {item.last[11:19]}")
    if replaying:
        print(f"Watchdog copies held={sum(item.watchdog_held for item in totals.values())}")
        print(f"Replay: held={sum(item.held for item in totals.values())}; "
              + f"distinct held={sum(item.distinct_held for item in totals.values())}; "
              + f"person/user pushes held={sum(item.protected_held for item in totals.values())}")
        print(f"Coverage: independently unlabelled={sum(item.unlabelled for item in totals.values())}; "
              + f"unknown historical human provenance={sum(item.unknown_human for item in totals.values())}. "
              + "Unlabelled distinctness is unknown, not proven safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
