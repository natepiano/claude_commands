"""Read Cargo folder settings without loading a build or sweep caller."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import cast


class CargoConfigPolicy(Enum):
    COMPATIBLE = auto()
    CURRENT_STRICT = auto()


@dataclass(frozen=True)
class CargoFolderSetting:
    value: str
    base: Path


def cargo_path_argument(argv: list[str], flag: str) -> str:
    for index, arg in enumerate(argv):
        if arg.startswith(flag + "="):
            return arg.split("=", 1)[1]
        if arg == flag and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def cargo_config_values(argv: list[str]) -> list[str]:
    """Read each nonempty Cargo config argument in command order."""
    values: list[str] = []
    for index, arg in enumerate(argv):
        value = arg.split("=", 1)[1] if arg.startswith("--config=") else (
            argv[index + 1] if arg == "--config" and index + 1 < len(argv) else "")
        if value:
            values.append(value)
    return values


def cargo_home_folder(cwd: str, environment: Mapping[str, str]) -> str:
    """Resolve Cargo home against the command's working folder."""
    cargo_home = environment.get("CARGO_HOME")
    if not cargo_home and environment.get("HOME"):
        cargo_home = os.path.join(environment["HOME"], ".cargo")
    return os.path.realpath(os.path.join(cwd, cargo_home)) if cargo_home else ""


def cargo_config_folders(cwd: str, environment: Mapping[str, str]) -> list[Path]:
    """List Cargo home and the ancestor config folders in precedence order."""
    cargo_home = cargo_home_folder(cwd, environment)
    folders = [Path(cargo_home)] if cargo_home else []
    directory = Path(cwd)
    folders.extend(parent / ".cargo" for parent in reversed((directory, *directory.parents)))
    return folders


def cargo_folder_settings(argv: list[str], cwd: str, environment: Mapping[str, str], *,
                          policy: CargoConfigPolicy = CargoConfigPolicy.COMPATIBLE) -> dict[str, CargoFolderSetting]:
    """Apply Cargo precedence to folder settings under both policies.

    Read config files, then CARGO_BUILD_TARGET_DIR and CARGO_BUILD_BUILD_DIR, then CLI config.
    Apply CARGO_TARGET_DIR next, then the first --target-dir argument.
    Strict mode reads current config files and raises for invalid or unreadable CLI config.
    """
    strict = policy is CargoConfigPolicy.CURRENT_STRICT
    directory = Path(cwd)
    settings: dict[str, CargoFolderSetting] = {}

    def read_settings(contents: str, base: Path) -> None:
        try:
            table = cast(dict[str, object], tomllib.loads(contents)).get("build")
        except tomllib.TOMLDecodeError:
            if strict:
                raise
            return
        if isinstance(table, dict):
            for key in ("target-dir", "build-dir"):
                value = cast(dict[str, object], table).get(key)
                if isinstance(value, str) and value:
                    settings[key] = CargoFolderSetting(value, base)

    def read_config(path: Path) -> None:
        try:
            read_settings(path.read_text(), path.parent.parent)
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            if strict:
                raise

    for config in cargo_config_folders(cwd, environment):
        legacy = config / "config"
        read_config(legacy if not strict and legacy.is_file() else config / "config.toml")
    for key, variable in (("target-dir", "CARGO_BUILD_TARGET_DIR"), ("build-dir", "CARGO_BUILD_BUILD_DIR")):
        if environment.get(variable):
            settings[key] = CargoFolderSetting(environment[variable], directory)
    for setting in cargo_config_values(argv):
        if "=" in setting:
            read_settings(setting, directory)
        elif strict:
            path = directory / setting
            read_settings(path.read_text(), path.parent.parent)
        else:
            read_config(directory / setting)
    if environment.get("CARGO_TARGET_DIR"):
        settings["target-dir"] = CargoFolderSetting(environment["CARGO_TARGET_DIR"], directory)
    named = cargo_path_argument(argv, "--target-dir")
    if named:
        settings["target-dir"] = CargoFolderSetting(named, directory)
    return settings


def cargo_workspace_manifest(argv: list[str], cwd: str, cargo_home: str = "") -> Path:
    """Find the workspace manifest without starting Cargo or reading its cache.

    Cargo core/workspace.rs at 58bab51c: find_root_iter and is_excluded.
    """
    directory = Path(cwd)
    named = cargo_path_argument(argv, "--manifest-path")
    if named:
        manifest = Path(os.path.abspath(directory / named))
    else:
        manifest = next((parent / "Cargo.toml" for parent in (directory, *directory.parents)
                         if (parent / "Cargo.toml").is_file()), directory / "Cargo.toml")
    table = cast(dict[str, object], tomllib.loads(manifest.read_text()))
    if isinstance(table.get("workspace"), dict):
        return manifest
    package = table.get("package")
    if isinstance(package, dict):
        workspace = cast(dict[str, object], package).get("workspace")
        if isinstance(workspace, str):
            root = manifest.parent / workspace / "Cargo.toml"
            root_table = cast(dict[str, object], tomllib.loads(root.read_text()))
            if isinstance(root_table.get("workspace"), dict):
                return root
            raise ValueError("Cargo package.workspace does not name a workspace")
    for parent in manifest.parent.parents:
        if parent.parts[-2:] == ("target", "package"):
            break
        if cargo_home and manifest.is_relative_to(cargo_home) and not parent.is_relative_to(cargo_home):
            break
        root = parent / "Cargo.toml"
        if not root.is_file():
            continue
        table = cast(dict[str, object], tomllib.loads(root.read_text()))
        workspace = table.get("workspace")
        if isinstance(workspace, dict):
            matches: dict[str, bool] = {}
            for key in ("exclude", "members"):
                paths = cast(dict[str, object], workspace).get(key, [])
                if not isinstance(paths, list) or any(not isinstance(path, str) for path in cast(list[object], paths)):
                    raise ValueError("Cargo workspace membership cannot be read")
                matches[key] = any(manifest.is_relative_to(parent / path) for path in cast(list[str], paths))
            if matches["exclude"] and not matches["members"]:
                continue
            return root
        package = table.get("package")
        if isinstance(package, dict) and "workspace" in package:
            raise ValueError("An ancestor Cargo manifest redirects workspace discovery")
    return manifest
