"""Cargo build shapes and their read-only join to older admission peaks.

A shape separates profile, feature selection and target selection. It does not
claim that a cached build and a full compile have the same peak; that split is
separate from this key. Rows without valid argv or a call id prove no shape.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class BuildShape:
    profile: str
    features: tuple[str, ...]
    targets: tuple[str, ...]

    @property
    def key(self) -> str:
        """A stable, unambiguous value for admission and history records."""
        return json.dumps((self.profile, self.features, self.targets), separators=(",", ":"))


@dataclass(frozen=True)
class NoBuildShape:
    """A row has no usable evidence of its Cargo shape."""


def recorded_build_shape(raw: object) -> BuildShape | NoBuildShape:
    """Decode the stable key at the record boundary; never infer a dev shape."""
    if not isinstance(raw, str):
        return NoBuildShape()
    try:
        decoded = cast(object, json.loads(raw))
        if not isinstance(decoded, list) or len(cast(list[object], decoded)) != 3:
            return NoBuildShape()
        profile, features, targets = cast(list[object], decoded)
        if not isinstance(profile, str) or not profile:
            return NoBuildShape()
        if not isinstance(features, list) or not isinstance(targets, list) or not targets:
            return NoBuildShape()
        parts = cast(list[object], features) + cast(list[object], targets)
        if not all(isinstance(part, str) and part for part in parts):
            return NoBuildShape()
        return BuildShape(profile, tuple(sorted(set(cast(list[str], features)))),
                          tuple(sorted(set(cast(list[str], targets)))))
    except (ValueError, TypeError):
        return NoBuildShape()


def build_shape(argv: Sequence[str]) -> BuildShape:
    """Read Cargo's shape flags, stopping before arguments to the program."""
    profile = "dev"
    features: set[str] = set()
    targets: set[str] = set()
    arguments = iter(argv)
    for argument in arguments:
        if argument == "--":
            break
        flag, equals, value = argument.partition("=")
        if flag in ("--profile", "--features", "-F", "--bin", "--example"):
            if not equals:
                value = next(arguments, "")
            if not value or value.startswith("-"):
                raise ValueError(f"{flag} needs a value")
            if flag == "--profile":
                profile = value
            elif flag in ("--features", "-F"):
                features.update(value.replace(",", " ").split())
            else:
                targets.add(f"{flag[2:]}:{value}")
        elif argument == "--release":
            profile = "release"
        elif argument in ("--all-features", "--no-default-features"):
            features.add(argument)
        elif argument == "--lib":
            targets.add("lib")
    return BuildShape(profile, tuple(sorted(features)), tuple(sorted(targets)) or ("all",))


def index_shapes(index_path: Path, host: str, step: str,
                 cutoff: datetime, before: datetime) -> dict[tuple[str, str], BuildShape]:
    """Join (call id, step) to shape in [cutoff, before), by index start time.

    A call id follows one invocation across checkout folders. No index sync
    runs. A missing or old index supplies no join and is never
    created. The caller must still check a peak row's own repository and time.
    """
    shapes: dict[tuple[str, str], BuildShape] = {}
    try:
        with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            rows = cast(list[tuple[object, ...]], connection.execute(
                "SELECT call_id, step, argv, started_at FROM steps WHERE host=? AND step=?",
                (host, step),
            ).fetchall())
        for call_id, name, raw_argv, started_at in rows:
            if not isinstance(call_id, str) or not call_id or not isinstance(raw_argv, str):
                continue
            if not isinstance(name, str) or not isinstance(started_at, str):
                continue
            try:
                if not cutoff <= datetime.fromisoformat(started_at) < before:
                    continue
                decoded = cast(object, json.loads(raw_argv))
                if not isinstance(decoded, list) or not decoded:
                    continue
                words = cast(list[object], decoded)
                if not all(isinstance(word, str) for word in words):
                    continue
                shapes[call_id, name] = build_shape(cast(list[str], words))
            except (ValueError, TypeError):
                continue
    except (OSError, sqlite3.Error):
        pass
    return shapes
