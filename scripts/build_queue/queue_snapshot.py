"""Store captured queue evidence for callers that cannot read the live service."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import cast

from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue import admission_records


@dataclass(frozen=True)
class QueueSnapshot:
    written_at: float
    inputs: admission_records.QueueInputs
    policy: admission_records.AdmissionPolicy


@dataclass(frozen=True)
class NoQueueSnapshot:
    pass


def write_queue_snapshot(directory: Path, inputs: admission_records.QueueInputs, policy: admission_records.AdmissionPolicy) -> None:
    """Publish one completed pass atomically without additional memory readings."""
    encoded = json.dumps({"v": 5, "written_at": inputs.now, "inputs": codec.snapshot_value(inputs),
                          "policy": codec.snapshot_value(policy)},
                         separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    temporary = directory / "queue.json.tmp"
    try:
        _ = temporary.write_text(encoded)
        os.replace(temporary, directory / "queue.json")
    finally:
        temporary.unlink(missing_ok=True)


def read_queue_snapshot(directory: Path) -> QueueSnapshot | NoQueueSnapshot:
    """Refuse missing or incomplete evidence without reading the live queue."""
    try:
        raw = cast(object, json.loads((directory / "queue.json").read_text()))
        if not isinstance(raw, dict):
            return NoQueueSnapshot()
        envelope = cast(dict[str, object], raw)
        if type(envelope.get("v")) is not int or envelope["v"] != 5:
            return NoQueueSnapshot()
        written_at = cast(float, codec.parse_snapshot_value(envelope["written_at"], float))
        evidence = envelope["inputs"]
        if not isinstance(evidence, dict):
            return NoQueueSnapshot()
        fields = cast(dict[str, object], evidence)
        if "limits" in fields:
            return NoQueueSnapshot()
        inputs = cast(admission_records.QueueInputs, codec.parse_snapshot_value(fields, admission_records.QueueInputs))
        if sum(not isinstance(step.measured, (admission_records.CompilerServiceMemory, admission_records.UnownedSliceMemory))
               for step in inputs.running) != len(inputs.reservations):
            return NoQueueSnapshot()
        policy = cast(admission_records.AdmissionPolicy, codec.parse_snapshot_value(envelope["policy"], admission_records.AdmissionPolicy))
        return QueueSnapshot(written_at, inputs, policy)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return NoQueueSnapshot()
