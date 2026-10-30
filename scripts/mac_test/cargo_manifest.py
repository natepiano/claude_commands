"""Read Cargo manifests and find workspace member folders."""
from __future__ import annotations

import glob
from pathlib import Path
import tomllib
from typing import cast


def toml_table(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        decoded = cast(object, tomllib.load(handle))
    if not isinstance(decoded, dict):
        raise ValueError(f"{path} does not contain a TOML table")
    return cast(dict[str, object], decoded)


def nested_table(values: dict[str, object], key: str) -> dict[str, object]:
    value = values.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def string_sequence(values: dict[str, object], key: str) -> tuple[str, ...]:
    value = values.get(key)
    if not isinstance(value, list):
        return ()
    items = cast(list[object], value)
    return tuple(item for item in items if isinstance(item, str))


def within(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def expanded_directories(root: Path, patterns: tuple[str, ...]) -> set[Path]:
    directories: set[Path] = set()
    for pattern in patterns:
        for raw_path in glob.glob(str(root / pattern)):
            path = Path(raw_path).resolve()
            if within(root, path) and path.is_dir():
                directories.add(path)
    return directories


def member_directories(root: Path, manifest: dict[str, object]) -> tuple[Path, ...]:
    workspace = nested_table(manifest, "workspace")
    members = expanded_directories(root, string_sequence(workspace, "members"))
    excluded = expanded_directories(root, string_sequence(workspace, "exclude"))
    members.difference_update(excluded)
    if nested_table(manifest, "package"):
        members.add(root)
    return tuple(sorted(members))
