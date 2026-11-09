"""Select a target through routing or a fixed target name."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Routed:
    pass


@dataclass(frozen=True)
class Pinned:
    target: str


TargetSelection = Routed | Pinned
ROUTED = Routed()
