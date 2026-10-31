"""Identify a pane within a named herdr session without loading its controller."""
from dataclasses import dataclass


@dataclass(frozen=True)
class HerdrPlace:
    session: str
    pane: str
