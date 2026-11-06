"""Merge time spans and measure their combined duration."""

from collections.abc import Iterable


def merged_spans(spans: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Return sorted spans with overlapping and touching spans merged."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def union_seconds(spans: Iterable[tuple[float, float]]) -> float:
    """Return the duration covered by at least one span."""
    return sum((end - start for start, end in merged_spans(spans)), 0.0)
