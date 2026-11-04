"""Format recorded queue evidence without reading admission configuration."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from scripts.lint import memory_admit as gate
from scripts.message import unit_labels


def step_labels(step: gate.WaitingStep | gate.RunningStep,
                labels: Mapping[str, unit_labels.UnitLabels]) -> unit_labels.UnitLabels:
    identity = labels.get(step.session_id)
    unit = gate.queue_unit(step.unit, step.session_id, labels)
    if identity is not None:
        return replace(identity, unit=unit) if step.unit else identity
    fallback = " ".join(step.session_name.split()) or step.folder.rstrip("/").rpartition("/")[2] or "unknown"
    return unit_labels.UnitLabels(fallback, unit=unit or "—")

