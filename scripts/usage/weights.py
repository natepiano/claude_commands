"""Apply dated model weights to raw ledger rows when they are read."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ModelWeights:
    model: str
    effective_from: datetime
    cached: float
    cache_write_5m: float
    cache_write_1h: float
    output: float


MODEL_WEIGHTS = (
    ModelWeights("gpt-5.6-sol", datetime.min.replace(tzinfo=timezone.utc), 0.1, 0, 0, 8),
    ModelWeights("gpt-6.1-sol", datetime(2026, 10, 10, 21, 35, tzinfo=timezone.utc), 0.05, 0, 0, 5),
    # This entry covers the specified weights shared by every Claude model.
    ModelWeights("claude-*", datetime.min.replace(tzinfo=timezone.utc), 0.1, 1.25, 2, 5),
)


class UnweightedModel(ValueError):
    """No weight entry covers this model at the requested time."""


def weights_at(model: str, at: datetime | str) -> ModelWeights:
    moment = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else at
    moment = (moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None
              else moment.astimezone(timezone.utc))
    entries = [entry for entry in MODEL_WEIGHTS
               if (entry.model == model or entry.model == "claude-*" and model.startswith("claude-"))
               and entry.effective_from <= moment]
    if not entries:
        raise UnweightedModel(f"unweighted model {model!r} at {moment.isoformat()}")
    return max(entries, key=lambda entry: entry.effective_from)


def weighted(turn_row: Mapping[str, object] | sqlite3.Row, at: datetime | str) -> float:
    model: object = turn_row["model"]
    if not isinstance(model, str):
        raise UnweightedModel("unweighted: model was not reported")
    entry = weights_at(model, at)
    coefficients = (("input", 1.0), ("cache_read", entry.cached),
                    ("cache_write_5m", entry.cache_write_5m),
                    ("cache_write_1h", entry.cache_write_1h), ("output", entry.output))
    total = 0.0
    for column, coefficient in coefficients:
        count: object = turn_row[column]
        if isinstance(count, bool) or not isinstance(count, (int, float)) or count < 0:
            raise ValueError(f"invalid token count for {column}")
        total += count * coefficient
    return total
