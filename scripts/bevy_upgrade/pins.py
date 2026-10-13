"""Read Bevy versions and the project's dependent pins without running Cargo."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast
from urllib.parse import urlsplit, urlunsplit


class RegistryPin(TypedDict):
    source: Literal["registry"]
    version: str


class GitPin(TypedDict):
    source: Literal["git"]
    version: str
    url: str
    ref: str  # The lock source's query, or empty for its default branch.
    rev: str  # The full commit recorded after '#'.


class PathPin(TypedDict):
    source: Literal["path"]
    version: str
    path: str  # Preserve the manifest's spelling.


Pin = RegistryPin | GitPin | PathPin


class Dependent(NamedTuple):
    name: str
    pin: Pin
    bevy: str
    via: tuple[str, ...]  # Empty when a source-less package lists this dependent.
    patched_bevy: bool


class Pins(NamedTuple):
    bevy: tuple[str, ...]
    bevy_names: frozenset[str]
    dependents: tuple[Dependent, ...]


class PinsFailure(Exception):
    """The checkout cannot supply a complete set of current pins."""


class LockedPackage(TypedDict):
    name: str
    version: str
    source: str  # Empty for a workspace member or path crate.
    dependencies: list[str]


class ManifestPatch(TypedDict):
    path: str  # Empty for a git patch.
    git: str  # Empty for a path patch.


class SelectedDependent(NamedTuple):
    package: LockedPackage
    bevy: str
    patched_bevy: bool


def _toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            return cast(dict[str, object], tomllib.load(stream))
    except (OSError, ValueError) as error:
        raise PinsFailure(f"{path}: {error}") from error


def _packages(document: dict[str, object], path: Path) -> tuple[LockedPackage, ...]:
    raw_packages = document.get("package", [])
    if not isinstance(raw_packages, list):
        raise PinsFailure(f"{path}: package must be an array")
    packages: list[LockedPackage] = []
    for raw in cast(list[object], raw_packages):
        if not isinstance(raw, dict):
            raise PinsFailure(f"{path}: package must be a table")
        package = cast(dict[str, object], raw)
        name, version = package.get("name"), package.get("version")
        source, dependencies = package.get("source", ""), package.get("dependencies", [])
        if not isinstance(name, str) or not isinstance(version, str) or not isinstance(source, str):
            raise PinsFailure(f"{path}: package name, version and source must be strings")
        if not isinstance(dependencies, list):
            raise PinsFailure(f"{path}: {name} dependencies must be an array of strings")
        items = cast(list[object], dependencies)
        if not all(isinstance(item, str) for item in items):
            raise PinsFailure(f"{path}: {name} dependencies must be an array of strings")
        packages.append(LockedPackage(name=name, version=version, source=source,
                                      dependencies=cast(list[str], items)))
    return tuple(packages)


def _patches(document: dict[str, object], path: Path) -> dict[str, ManifestPatch]:
    raw = document.get("patch", {})
    if not isinstance(raw, dict):
        raise PinsFailure(f"{path}: patch must be a table")
    patches: dict[str, ManifestPatch] = {}
    for table in cast(dict[str, object], raw).values():
        if not isinstance(table, dict):
            raise PinsFailure(f"{path}: patch source must be a table")
        for name, value in cast(dict[str, object], table).items():
            if not isinstance(value, dict):
                raise PinsFailure(f"{path}: patch {name} must be a table")
            fields = cast(dict[str, object], value)
            patch_path, git = fields.get("path", ""), fields.get("git", "")
            if not isinstance(patch_path, str) or not isinstance(git, str):
                raise PinsFailure(f"{path}: patch {name} path and git must be strings")
            patches[name] = ManifestPatch(path=patch_path, git=git)
    return patches


def _version_key(version: str) -> tuple[tuple[int, ...], int, tuple[tuple[int, int, str], ...]]:
    core, _, prerelease = version.split("+", 1)[0].partition("-")
    identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                        for part in prerelease.split(".")) if prerelease else ()
    return tuple(int(part) for part in core.split(".")), int(not prerelease), identifiers


def _identity(package: LockedPackage) -> tuple[str, str, str]:
    return package["name"], package["version"], package["source"]


def _resolve(dependency: str, packages: dict[str, list[LockedPackage]], path: Path) -> LockedPackage:
    match = re.fullmatch(r"(\S+)(?: (\S+))?(?: \((.+)\))?", dependency)
    if match is None:
        raise PinsFailure(f"{path}: invalid dependency {dependency!r}")
    name, version, source = match.groups()
    candidates = [package for package in packages.get(name, [])
                  if (version is None or package["version"] == version)
                  and (source is None or package["source"] == source)]
    if len(candidates) != 1:
        raise PinsFailure(f"{path}: dependency {dependency!r} does not resolve to one package")
    return candidates[0]


def _is_patched(package: LockedPackage, patches: dict[str, ManifestPatch]) -> bool:
    patch = patches.get(package["name"])
    if patch is None:
        return False
    source = package["source"]
    return (not source and bool(patch["path"])) or (source.startswith("git+") and bool(patch["git"]))


def _pin(package: LockedPackage, patches: dict[str, ManifestPatch], path: Path) -> Pin:
    source = package["source"]
    if source.startswith("registry+"):
        return RegistryPin(source="registry", version=package["version"])
    if source.startswith("git+"):
        url = urlsplit(source.removeprefix("git+"))
        return GitPin(source="git", version=package["version"],
                      url=urlunsplit((url.scheme, url.netloc, url.path, "", "")),
                      ref=url.query, rev=url.fragment)
    patch = patches.get(package["name"])
    if not source and patch is not None and patch["path"]:
        return PathPin(source="path", version=package["version"], path=patch["path"])
    raise PinsFailure(f"{path}: {package['name']} has no registry, git or path pin")


def read_pins(checkout: Path) -> Pins:
    lock_path, manifest_path = checkout / "Cargo.lock", checkout / "Cargo.toml"
    packages = _packages(_toml(lock_path), lock_path)
    patches = _patches(_toml(manifest_path), manifest_path)
    versions = {package["version"] for package in packages if package["name"] == "bevy_ecs"}
    if not versions:
        raise PinsFailure(f"{lock_path}: no bevy_ecs package, so this is not a Bevy project")
    try:
        bevy = tuple(sorted(versions, key=_version_key))
    except ValueError as error:
        raise PinsFailure(f"{lock_path}: invalid Bevy version: {error}") from error
    bevy_packages = {_identity(package) for package in packages
                     if (package["name"] == "bevy" or package["name"].startswith("bevy_"))
                     and package["version"] in versions
                     and (package["source"].startswith("registry+") or _is_patched(package, patches))}
    by_name: dict[str, list[LockedPackage]] = {}
    for package in packages:
        by_name.setdefault(package["name"], []).append(package)
    edges = {_identity(package): tuple(_resolve(dependency, by_name, lock_path)
                                      for dependency in package["dependencies"])
             for package in packages}
    parents: dict[tuple[str, str, str], list[LockedPackage]] = {}
    for package in packages:
        for dependency in edges[_identity(package)]:
            parents.setdefault(_identity(dependency), []).append(package)
    selected: dict[str, SelectedDependent] = {}
    for package in packages:
        identity = _identity(package)
        patched = _is_patched(package, patches)
        patched_bevy = identity in bevy_packages and patched
        if identity in bevy_packages and not patched_bevy:
            continue
        if not package["source"] and not patched:
            continue
        bevy_dependencies = [dependency["version"] for dependency in edges[identity]
                             if _identity(dependency) in bevy_packages]
        if not patched_bevy and not bevy_dependencies:
            continue
        resolved_bevy = package["version"] if patched_bevy else min(bevy_dependencies, key=_version_key)
        previous = selected.get(package["name"])
        if previous is None or _version_key(resolved_bevy) < _version_key(previous.bevy):
            selected[package["name"]] = SelectedDependent(package, resolved_bevy, patched_bevy)
    dependents: list[Dependent] = []
    for name in sorted(selected):
        package, resolved_bevy, patched_bevy = selected[name]
        identity = _identity(package)
        listed_by = parents.get(identity, [])
        via = () if any(not parent["source"] for parent in listed_by) else tuple(
            sorted({parent["name"] for parent in listed_by}))
        dependents.append(Dependent(name, _pin(package, patches, lock_path), resolved_bevy,
                                    via, patched_bevy))
    return Pins(bevy, frozenset(identity[0] for identity in bevy_packages),
                tuple(dependents))
