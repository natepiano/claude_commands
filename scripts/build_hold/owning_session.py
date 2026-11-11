"""Identify the session that owns a build, or the absence of that session."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OwningSession:
    session_id: str


@dataclass(frozen=True)
class NoOwningSession:
    pass
