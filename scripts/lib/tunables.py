"""Read configured values without substituting a missing or unusable setting."""
from __future__ import annotations

import math
from pathlib import Path


class TunableUnusable(ValueError):
    """A configuration file cannot supply the requested setting."""


def values(path: Path) -> dict[str, str]:
    """Read KEY=value statements, with comments starting at #."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise TunableUnusable(f"{path}: the file cannot be read ({error})") from error
    result: dict[str, str] = {}
    for line in text.splitlines():
        statement = line.split("#", 1)[0].strip()
        key, separator, value = statement.partition("=")
        if separator:
            result[key.strip()] = value.strip()
    return result


def number(path: Path, key: str, *, minimum: float, maximum: float) -> float:
    """Read a finite number within the caller's permitted bounds."""
    try:
        configured = values(path)
    except TunableUnusable as error:
        raise TunableUnusable(f"{path}: {key}: {error}") from error
    raw = configured.get(key)
    if raw is None:
        raise TunableUnusable(f"{path}: {key} is not set")
    try:
        parsed = float(raw)
    except ValueError as error:
        raise TunableUnusable(f"{path}: {key}={raw!r} is not a number") from error
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise TunableUnusable(f"{path}: {key}={raw!r} must be between {minimum} and {maximum}")
    return parsed
