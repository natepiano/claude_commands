"""Read distinct step output facts without applying a caller's policy."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, cast

if __package__:
    from . import store
else:
    import store

if TYPE_CHECKING:
    from ..lib.cargo_target_config import cargo_config_values, cargo_path_argument
else:
    from scripts.lib.cargo_target_config import cargo_config_values, cargo_path_argument


@dataclass(frozen=True)
class AbsoluteRecordedRoot:
    """A step records an absolute path."""

    path: Path


@dataclass(frozen=True)
class UnknownRecordedRoot:
    """A step records no path."""


@dataclass(frozen=True)
class RelativeRecordedRoot:
    """A step records a relative path."""


RecordedRoot = AbsoluteRecordedRoot | UnknownRecordedRoot | RelativeRecordedRoot


class TargetFolderOverride(Enum):
    """A step command keeps or overrides its recorded target folder."""

    ABSENT = auto()
    PRESENT = auto()


@dataclass(frozen=True)
class RecordedStepOutput:
    """One distinct row preserves its output paths and workspace facts."""

    worktree: RecordedRoot
    cwd: RecordedRoot
    target_directory: RecordedRoot
    build_directory: RecordedRoot
    target_folder: RecordedRoot
    target_folder_override: TargetFolderOverride


@dataclass(frozen=True)
class RecordedStepOutputs:
    """The distinct step output rows for one host."""

    rows: tuple[RecordedStepOutput, ...]


@dataclass(frozen=True)
class RecordedRootsUnavailable:
    """The index cannot supply recorded output roots."""

    reason: str


def _recorded_root(raw: object) -> RecordedRoot:
    """Read a path without resolving it against the current checkout."""
    if not isinstance(raw, str) or not raw or raw == store.UNKNOWN_IDENTITY:
        return UnknownRecordedRoot()
    path = Path(raw)
    return AbsoluteRecordedRoot(path) if path.is_absolute() else RelativeRecordedRoot()


def indexed_argv(raw: object) -> list[str]:
    """Decode an indexed command, or return no arguments for invalid data."""
    if not isinstance(raw, str):
        return []
    try:
        decoded = cast(object, json.loads(raw))
    except ValueError:
        return []
    if not isinstance(decoded, list) or not all(isinstance(word, str) for word in cast(list[object], decoded)):
        return []
    return cast(list[str], decoded)


def _sets_target_folder(raw_argv: object) -> bool:
    """Read target-folder overrides with the shared Cargo flag reader."""
    words = indexed_argv(raw_argv)
    if cargo_path_argument(words, "--target-dir") or "--target-dir" in words:
        return True
    return any("target-dir" in value for value in cargo_config_values(words))


def recorded_output_roots(index_path: Path, host: str) -> RecordedStepOutputs | RecordedRootsUnavailable:
    """Read distinct row facts without metadata calls or index writes."""
    try:
        with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.create_function("sets_target_folder", 1, _sets_target_folder, deterministic=True)
            rows = cast(list[tuple[object, ...]], connection.execute(
                """SELECT DISTINCT worktree, cwd, target_directory, build_directory, target_folder,
                       CASE WHEN instr(argv, 'target-dir') > 0 THEN sets_target_folder(argv) ELSE 0 END
                   FROM steps WHERE host = ?""",
                (host,),
            ).fetchall())
    except (OSError, sqlite3.Error) as error:
        return RecordedRootsUnavailable(str(error))
    return RecordedStepOutputs(tuple(RecordedStepOutput(
        worktree=_recorded_root(worktree),
        cwd=_recorded_root(cwd),
        target_directory=_recorded_root(target_path),
        build_directory=_recorded_root(build_path),
        target_folder=_recorded_root(target_folder),
        target_folder_override=TargetFolderOverride.PRESENT if override_value else TargetFolderOverride.ABSENT,
    ) for worktree, cwd, target_path, build_path, target_folder, override_value in rows))
