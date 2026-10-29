"""Read routed findings and join their receiving checkpoints across run files."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import TypedDict

from scripts.history.history import (
    SPINE_VERSION, _arg, _duration, _integer, _meta, _number, _object,
    _since_epoch, _stores, _string, _warn,
)


class RoutedWork(TypedDict):
    """An outgoing finding and the evidence from its receiving checkpoint."""

    key: str
    run: str
    plan: str
    phase: str
    phase_instance_id: str
    finding: str
    owner: str
    work_order: str
    due: str
    reason: str
    evidence: str
    repair_round: int
    round: int
    fix_attempts: int
    astra_approach: str
    timestamp: str
    timestamp_epoch: float
    age_seconds: float
    age: str
    status: str
    closure: dict[str, object]


def routed_work(records: list[tuple[str, dict[str, object]]], now: float) -> list[RoutedWork]:
    """Join on source run, phase instance and finding; the closure can be in any run."""
    runs: dict[str, dict[str, object]] = {}
    phases: dict[tuple[str, str], dict[str, object]] = {}
    closures: dict[str, dict[str, object]] = {}
    for run, event in records:
        kind = _string(event.get("event_type"))
        if kind == "run_started":
            runs[run] = event
        elif kind == "phase_started":
            phases[(run, _string(event.get("phase_instance_id")))] = event
        elif kind == "finding_route_closed":
            key = _string(event.get("from"))
            if key and _number(event.get("timestamp_epoch")) >= _number(closures.get(key, {}).get("timestamp_epoch")):
                closures[key] = {**event, "run": run}
    rows: list[RoutedWork] = []
    for run, event in records:
        if _string(event.get("event_type")) != "finding_routed":
            continue
        instance = _string(event.get("phase_instance_id"))
        finding = _string(event.get("finding_id"))
        key = f"{run}:{instance}:{finding}"
        seed = runs.get(run, {})
        phase = phases.get((run, instance), {})
        stamp = _number(event.get("timestamp_epoch"))
        age = max(0.0, now - stamp)
        closure = closures.get(key, {})
        rows.append(RoutedWork(
            key=key, run=run,
            plan=Path(_string(seed.get("project_plan_doc"), _string(seed.get("plan_doc")))).name,
            phase=_string(event.get("phase_id"), _string(phase.get("phase_id"))),
            phase_instance_id=instance, finding=finding,
            owner=_string(event.get("owner")), work_order=_string(event.get("work_order")),
            due=_string(event.get("due")), reason=_string(event.get("reason")),
            evidence=_string(event.get("evidence")), repair_round=_integer(event.get("repair_round")),
            round=_integer(event.get("round")), fix_attempts=_integer(event.get("fix_attempts")),
            astra_approach=_string(event.get("astra_approach")), timestamp=_string(event.get("timestamp")),
            timestamp_epoch=stamp, age_seconds=age, age=_duration(age),
            status="closed" if closure else "open", closure=closure,
        ))
    rows.sort(key=lambda row: (row["timestamp_epoch"], row["key"]))
    return rows


def run(args: argparse.Namespace) -> None:
    """Report all routes; --since filters outgoing work, never its closure lookup."""
    records: list[tuple[str, dict[str, object]]] = []
    tally = {"unreadable": 0, "future_spine": 0, "unknown_stage": 0}
    for _skill, directory in _stores():
        for path in sorted(directory.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                tally["unreadable"] += 1
                continue
            for line in lines:
                if not line.strip():
                    continue
                event = _object(line)
                if event is None:
                    tally["unreadable"] += 1
                elif _integer(event.get("spine_version")) > SPINE_VERSION:
                    tally["future_spine"] += 1
                else:
                    records.append((path.stem, event))
    since = _since_epoch(_arg(args, "since"))
    only_open = bool(getattr(args, "open", False))
    rows = [row for row in routed_work(records, datetime.now(UTC).timestamp())
            if row["timestamp_epoch"] >= since and (not only_open or row["status"] == "open")]
    if _arg(args, "format", "text") == "json":
        print(json.dumps({"_meta": _meta(tally), "rows": rows}, indent=2))
    else:
        print("run  plan  phase  finding  owner  work order  due checkpoint  age  closure")
        for row in rows:
            closure = _string(row["closure"].get("evidence")) if row["closure"] else "open"
            print("  ".join([row["run"], row["plan"], row["phase"], row["finding"], row["owner"],
                             row["work_order"], row["due"], row["age"], closure]))
        print(f"{len(rows)} routed findings")
    _warn(tally)
