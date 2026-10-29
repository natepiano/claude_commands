"""Load changeable display names. Keys remain record and routing contracts."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
import sys
import tomllib
from typing import Literal, cast, get_args

JobKey = Literal["showrunner", "unit_director", "worker", "implementer", "tester", "repair_worker", "code_reviewer",
                 "ui_reviewer", "designer", "design_reviewer"]
AudienceKey = Literal["showrunners", "unit_directors", "agents", "workers", "top_level", "all_roles"]
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config/names.toml"


@dataclass(frozen=True)
class UnknownKey:
    group: str
    key: str


@dataclass(frozen=True)
class MissingKey:
    group: str
    key: str


@dataclass(frozen=True)
class MissingDisplayName:
    group: str
    key: str


@dataclass(frozen=True)
class DuplicateDisplayName:
    group: str
    display: str


@dataclass(frozen=True)
class UnreadableNames:
    path: str
    reason: str


@dataclass(frozen=True)
class InvalidNames:
    path: str
    reason: str


NamesError = UnknownKey | MissingKey | MissingDisplayName | DuplicateDisplayName | UnreadableNames | InvalidNames


@dataclass(frozen=True)
class Names:
    jobs: Mapping[JobKey, tuple[str, ...]]
    audiences: Mapping[AudienceKey, tuple[str, ...]]

    def job(self, key: JobKey) -> str:
        return self.jobs[key][0]

    def audience(self, key: AudienceKey) -> str:
        return self.audiences[key][0]

    def retired(self) -> tuple[str, ...]:
        return tuple(name for group in (self.jobs, self.audiences)
                     for history in group.values() for name in history[1:])

    def current(self) -> tuple[str, ...]:
        return tuple(history[0] for group in (self.jobs, self.audiences)
                     for history in group.values())


def load_names(path: Path = CONFIG_PATH) -> Names | NamesError:
    try:
        raw = cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError) as error:
        return UnreadableNames(str(path), str(error))
    except tomllib.TOMLDecodeError as error:
        return InvalidNames(str(path), str(error))
    expected: dict[str, tuple[str, ...]] = {
        "jobs": cast(tuple[str, ...], get_args(JobKey)),
        "audiences": cast(tuple[str, ...], get_args(AudienceKey)),
    }
    for group in raw:
        if group not in expected:
            return UnknownKey(group, group)
    groups: dict[str, dict[str, tuple[str, ...]]] = {}
    seen: set[str] = set()
    for group, keys in expected.items():
        if group not in raw:
            return MissingKey(group, keys[0])
        table = raw[group]
        if not isinstance(table, dict):
            return InvalidNames(str(path), f"{group}: expected a table")
        entries = cast(dict[str, object], table)
        for key in entries:
            if key not in keys:
                return UnknownKey(group, key)
        histories: dict[str, tuple[str, ...]] = {}
        for key in keys:
            if key not in entries:
                return MissingKey(group, key)
            value = entries[key]
            if not isinstance(value, list) or not value:
                return MissingDisplayName(group, key)
            history: list[str] = []
            for name in cast(list[object], value):
                if not isinstance(name, str) or not name.strip():
                    return MissingDisplayName(group, key)
                if name.casefold() in seen:
                    return DuplicateDisplayName(group, name)
                seen.add(name.casefold())
                history.append(name)
            histories[key] = tuple(history)
        groups[group] = histories
    return Names(cast(Mapping[JobKey, tuple[str, ...]], groups["jobs"]),
                 cast(Mapping[AudienceKey, tuple[str, ...]], groups["audiences"]))


_reported_errors: set[NamesError] = set()


def error_text(error: NamesError, path: Path = CONFIG_PATH) -> str:
    """Describe a configuration problem with its source and the entry to fix."""
    if isinstance(error, UnknownKey):
        detail = f"[{error.group}] has an unknown {error.key} entry"
    elif isinstance(error, MissingKey):
        detail = f"[{error.group}] has no {error.key} entry"
    elif isinstance(error, MissingDisplayName):
        detail = f"[{error.group}].{error.key} needs a nonempty list of display names"
    elif isinstance(error, DuplicateDisplayName):
        detail = f"[{error.group}] repeats the display name {error.display!r}"
    else:
        path = Path(error.path)
        detail = f"cannot read the names: {error.reason}" if isinstance(error, UnreadableNames) else f"invalid names: {error.reason}"
    return f"names: {path}: {detail}"


def report_error(error: NamesError) -> None:
    """Report each config problem once in this process."""
    if error not in _reported_errors:
        _reported_errors.add(error)
        print(error_text(error), file=sys.stderr)


@cache
def runtime_names() -> Names | NamesError:
    """Load the command's configuration once; explicit path loads remain uncached."""
    loaded = load_names()
    if not isinstance(loaded, Names):
        report_error(loaded)
    return loaded
