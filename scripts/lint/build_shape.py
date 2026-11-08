"""Read Cargo shapes, compile counts, and test arguments.

Shape and compile evidence remain independent. The join keeps test arguments
for readers that must recover missing selection fields in anonymous peaks.
Only build rows supply a shape. Rows without valid arguments prove no shape.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from scripts.lib.test_phase import TEST_STEPS as TEST_STEPS


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


@dataclass(frozen=True)
class CompiledCrates:
    count: int


@dataclass(frozen=True)
class NoCratesCompiled:
    """The known attempts compiled no crates."""


@dataclass(frozen=True)
class CompileCountUnknown:
    """The index has no usable compile count."""


def combine_compile_counts(first: CompiledCrates | NoCratesCompiled | CompileCountUnknown,
                           second: CompiledCrates | NoCratesCompiled | CompileCountUnknown
                           ) -> CompiledCrates | NoCratesCompiled | CompileCountUnknown:
    """Keep positive counts and preserve uncertainty across attempts."""
    if isinstance(first, CompiledCrates) or isinstance(second, CompiledCrates):
        return CompiledCrates((first.count if isinstance(first, CompiledCrates) else 0)
                              + (second.count if isinstance(second, CompiledCrates) else 0))
    if isinstance(first, CompileCountUnknown) or isinstance(second, CompileCountUnknown):
        return CompileCountUnknown()
    return NoCratesCompiled()


@dataclass(frozen=True)
class StepEvidence:
    shape: BuildShape | NoBuildShape
    compilation: CompiledCrates | NoCratesCompiled | CompileCountUnknown
    arguments: tuple[str, ...] = ()


@contextmanager
def step_index(index_path: Path) -> Generator[tuple[sqlite3.Connection, set[str]]]:
    """Open the index without writes and read the available step columns."""
    with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        columns = {str(row[1]) for row in cast(list[tuple[object, ...]],
                   connection.execute("PRAGMA table_info(steps)").fetchall())}
        yield connection, columns


def index_step_evidence(index_path: Path, host: str, steps: tuple[str, ...],
                        call_ids: tuple[str, ...], before: datetime) -> dict[tuple[str, str], StepEvidence]:
    """Read build shapes, compile evidence and test arguments for each call and step.

    The caller supplies call ids from peaks in its completion window.
    Dated attempts at or after before do not change earlier evidence.
    Missing columns preserve the facts that the index can supply.
    This join does not parse test selections.
    """
    evidence: dict[tuple[str, str], StepEvidence] = {}
    if not steps or not call_ids:
        return evidence
    try:
        with step_index(index_path) as (connection, columns):
            argv_column = "argv" if "argv" in columns else "NULL"
            count_column = "crates_compiled" if "crates_compiled" in columns else "NULL"
            rows = cast(list[tuple[object, ...]], connection.execute(
                f"SELECT call_id, step, {argv_column}, started_at, {count_column} "
                + "FROM steps WHERE call_id IN (SELECT value FROM json_each(?)) "
                + "AND +host=? AND step IN (SELECT value FROM json_each(?))",
                (json.dumps(call_ids), host, json.dumps(steps)),
            ).fetchall())
        for call_id, name, raw_argv, started_at, count in rows:
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
                continue
            dated = False
            try:
                if isinstance(started_at, str):
                    if datetime.fromisoformat(started_at) >= before:
                        continue
                    dated = True
            except (ValueError, TypeError):
                pass
            compilation: CompiledCrates | NoCratesCompiled | CompileCountUnknown = CompileCountUnknown()
            if dated and isinstance(count, int) and not isinstance(count, bool):
                if count > 0:
                    compilation = CompiledCrates(count)
                elif count == 0:
                    compilation = NoCratesCompiled()
            shape: BuildShape | NoBuildShape = NoBuildShape()
            arguments: tuple[str, ...] = ()
            try:
                decoded = cast(object, json.loads(raw_argv)) if isinstance(raw_argv, str) else None
                if isinstance(decoded, list) and decoded:
                    words = cast(list[object], decoded)
                    for word in words:
                        if not isinstance(word, str):
                            break
                    else:
                        decoded_arguments = cast(list[str], words)
                        if name in TEST_STEPS:
                            arguments = tuple(decoded_arguments)
                        if name == "build":
                            shape = build_shape(decoded_arguments)
            except (ValueError, TypeError):
                pass
            previous = evidence.get((call_id, name))
            if previous is not None:
                compilation = combine_compile_counts(previous.compilation, compilation)
                if isinstance(shape, NoBuildShape):
                    shape = previous.shape
                if not arguments:
                    arguments = previous.arguments
            evidence[call_id, name] = StepEvidence(shape, compilation, arguments)
    except (OSError, sqlite3.Error):
        pass
    return evidence
