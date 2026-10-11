"""Statistics and API-equivalent prices for the director model comparison."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from turns import Turn


@dataclass(frozen=True)
class TokenPrices:
    input: float
    output: float
    write_5m: float
    write_1h: float
    cache_read: float


PRICES: dict[str, TokenPrices] = {
    "claude-opus-5-5": TokenPrices(4, 20, 5, 8, 0.20),
    "claude-sonnet-5-5": TokenPrices(2, 10, 2.50, 4, 0.20),
}


def percentile(values: Sequence[float | int], p: float) -> float:
    """Return a linearly interpolated percentile for p in [0, 100]."""
    if not values or not 0 <= p <= 100:
        raise ValueError("percentile needs values and p in [0, 100]")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    lower = int(rank)
    fraction = rank - lower
    if fraction == 0:
        return float(ordered[lower])
    return ordered[lower] + (ordered[lower + 1] - ordered[lower]) * fraction


def bootstrap_diff(
    a: Sequence[float | int],
    b: Sequence[float | int],
    stat: Callable[[Sequence[float | int]], float],
    resamples: int = 2000,
    seed: int = 1,
) -> tuple[float, float, float]:
    """Return b minus a and its seeded, percentile 95% bootstrap interval."""
    if not a or not b or resamples < 1:
        raise ValueError("bootstrap needs two nonempty samples and resamples > 0")
    generator = random.Random(seed)
    differences: list[float] = []
    for _ in range(resamples):
        sampled_a = generator.choices(a, k=len(a))
        sampled_b = generator.choices(b, k=len(b))
        differences.append(stat(sampled_b) - stat(sampled_a))
    return stat(b) - stat(a), percentile(differences, 2.5), percentile(differences, 97.5)


def bootstrap_net(
    opus: Sequence[float | int],
    sonnet: Sequence[float | int],
    control_before: Sequence[float | int],
    control_after: Sequence[float | int],
    stat: Callable[[Sequence[float | int]], float],
    resamples: int = 2000,
    seed: int = 1,
) -> tuple[float, float, float]:
    """Return the switched change minus the concurrent control change."""
    if not opus or not sonnet or not control_before or not control_after or resamples < 1:
        raise ValueError("bootstrap needs four nonempty samples and resamples > 0")
    generator = random.Random(seed)
    nets: list[float] = []
    for _ in range(resamples):
        sampled_opus = generator.choices(opus, k=len(opus))
        sampled_sonnet = generator.choices(sonnet, k=len(sonnet))
        sampled_before = generator.choices(control_before, k=len(control_before))
        sampled_after = generator.choices(control_after, k=len(control_after))
        nets.append((stat(sampled_sonnet) - stat(sampled_opus))
                    - (stat(sampled_after) - stat(sampled_before)))
    value = (stat(sonnet) - stat(opus)) - (stat(control_after) - stat(control_before))
    return value, percentile(nets, 2.5), percentile(nets, 97.5)


def request_cost(turn: Turn) -> float:
    """Price one standard-speed request in USD; thinking is included in output."""
    price = PRICES.get(turn.model)
    if price is None:
        return 0.0
    return (
        turn.input * price.input
        + turn.output * price.output
        + turn.cache_read * price.cache_read
        + turn.write_5m * price.write_5m
        + turn.write_1h * price.write_1h
    ) / 1_000_000


def label_difference(lower: float, upper: float, n_a: int, n_b: int, metric: str) -> str:
    """Describe a b-minus-a interval, with 30 requests required in each arm."""
    if n_a < 30 or n_b < 30:
        return "too few"
    if lower <= 0 <= upper:
        return "no measurable difference"
    if upper < 0:
        return "faster" if metric == "seconds" else "lower"
    return "slower" if metric == "seconds" else "higher"
