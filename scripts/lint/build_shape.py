"""Read Cargo shapes, compile counts, test arguments, and recorded output roots.

Shape and compile evidence remain independent. The join keeps test arguments
for readers that must recover missing selection fields in anonymous peaks.
Rows without valid arguments prove no shape.
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

from scripts.buildlog import store
from scripts.lib.cargo_target_config import cargo_path_argument

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
def _step_index(index_path: Path) -> Generator[tuple[sqlite3.Connection, set[str]]]:
    """Open the index without writes and read the available step columns."""
    with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        columns = {str(row[1]) for row in cast(list[tuple[object, ...]],
                   connection.execute("PRAGMA table_info(steps)").fetchall())}
        yield connection, columns


def index_step_evidence(index_path: Path, host: str, step: str,
                        cutoff: datetime, before: datetime) -> dict[tuple[str, str], StepEvidence]:
    """Read shape, compile evidence and test arguments for each call and step.

    The caller filters peaks by repository and completion time.
    Missing columns preserve the facts that the index can supply.
    This join does not parse test selections.
    """
    evidence: dict[tuple[str, str], StepEvidence] = {}
    test_step = step in TEST_STEPS
    try:
        with _step_index(index_path) as (connection, columns):
            argv_column = "argv" if "argv" in columns else "NULL"
            count_column = "crates_compiled" if "crates_compiled" in columns else "NULL"
            rows = cast(list[tuple[object, ...]], connection.execute(
                f"SELECT call_id, step, {argv_column}, started_at, {count_column} "
                + "FROM steps WHERE host=? AND step=?", (host, step),
            ).fetchall())
        for call_id, name, raw_argv, started_at, count in rows:
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
                continue
            dated = False
            try:
                if isinstance(started_at, str):
                    if not cutoff <= datetime.fromisoformat(started_at) < before:
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
                        if test_step:
                            arguments = tuple(decoded_arguments)
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


@dataclass(frozen=True)
class AbsoluteRecordedRoot:
    """A step records an absolute output path."""

    path: Path


@dataclass(frozen=True)
class UnknownRecordedRoot:
    """A step records no output path."""


@dataclass(frozen=True)
class RelativeRecordedRoot:
    """A relative output path cannot authorize a floor deletion."""


RecordedRoot = AbsoluteRecordedRoot | UnknownRecordedRoot | RelativeRecordedRoot


@dataclass(frozen=True)
class RecordedOutputPair:
    """The output paths from one step attempt stay together."""

    target_directory: RecordedRoot
    build_directory: RecordedRoot


@dataclass(frozen=True)
class RecordedOutputRoots:
    """Step attempts grouped by their recorded workspace identity."""

    workspaces: dict[RecordedRoot, tuple[RecordedOutputPair, ...]]


@dataclass(frozen=True)
class RecordedRootsUnavailable:
    """The index cannot supply recorded output roots."""

    reason: str


def recorded_root(raw: object) -> RecordedRoot:
    """Read a path without resolving it against the current checkout."""
    if not isinstance(raw, str) or not raw or raw == store.UNKNOWN_IDENTITY:
        return UnknownRecordedRoot()
    path = Path(raw)
    return AbsoluteRecordedRoot(path) if path.is_absolute() else RelativeRecordedRoot()


def _sets_target_folder(raw_argv: object) -> bool:
    """Reject step overrides that the recorded metadata did not receive."""
    if not isinstance(raw_argv, str):
        return False
    try:
        decoded = cast(object, json.loads(raw_argv))
    except ValueError:
        return False
    if not isinstance(decoded, list) or not all(isinstance(word, str) for word in cast(list[object], decoded)):
        return False
    words = cast(list[str], decoded)
    if cargo_path_argument(words, "--target-dir") or "--target-dir" in words:
        return True
    return any("target-dir" in cargo_path_argument(words[position:], "--config")
               for position, word in enumerate(words) if word == "--config" or word.startswith("--config="))


def recorded_output_roots(index_path: Path, host: str) -> RecordedOutputRoots | RecordedRootsUnavailable:
    """Read every step attempt on this host without metadata or index updates."""
    workspaces: dict[RecordedRoot, list[RecordedOutputPair]] = {}
    try:
        with _step_index(index_path) as (connection, columns):
            target = "target_directory" if "target_directory" in columns else "NULL"
            build = "build_directory" if "build_directory" in columns else target
            argv = "argv" if "argv" in columns else "NULL"
            folder = "target_folder" if "target_folder" in columns else "NULL"
            rows = cast(list[tuple[object, ...]], connection.execute(
                f"SELECT DISTINCT worktree, cwd, {target}, {build}, {argv}, {folder} FROM steps WHERE host = ?", (host,),
            ).fetchall())
        for worktree, cwd, target_path, build_path, raw_argv, target_folder in rows:
            target_root = recorded_root(target_path)
            folder_root = recorded_root(target_folder)
            if (_sets_target_folder(raw_argv)
                    or isinstance(folder_root, AbsoluteRecordedRoot) and folder_root != target_root):
                continue
            workspace = recorded_root(worktree)
            if not isinstance(workspace, AbsoluteRecordedRoot):
                workspace = recorded_root(cwd)
            workspaces.setdefault(workspace, []).append(RecordedOutputPair(
                target_root, recorded_root(build_path)))
    except (OSError, sqlite3.Error) as error:
        return RecordedRootsUnavailable(str(error))
    return RecordedOutputRoots({workspace: tuple(attempts) for workspace, attempts in workspaces.items()})
