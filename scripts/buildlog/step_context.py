"""Capture a step's tree key and target folder without loading the writer."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cargo_target_config import CargoConfigPolicy, cargo_folder_settings, cargo_home_folder, cargo_workspace_manifest
if __package__:
    from . import parse, store
else:
    import parse
    import store


@dataclass(frozen=True)
class CargoTargetFolder:
    path: Path

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("Cargo target folder must be absolute")


@dataclass(frozen=True)
class NoTargetFolder:
    pass


@dataclass(frozen=True)
class UnresolvedTargetFolder:
    pass


StepTargetFolder = CargoTargetFolder | NoTargetFolder | UnresolvedTargetFolder


def target_folder_text(folder: StepTargetFolder) -> str:
    if isinstance(folder, CargoTargetFolder):
        return str(folder.path)
    return "none" if isinstance(folder, NoTargetFolder) else "unresolved"


def captured_target_folder(value: str) -> StepTargetFolder:
    if value == "none":
        return NoTargetFolder()
    if value and Path(value).is_absolute():
        return CargoTargetFolder(Path(value))
    return UnresolvedTargetFolder()


def metadata_target_folder(metadata: str, argv: list[str], cwd: str) -> StepTargetFolder:
    """Accept metadata only for the selected manifest or working folder."""
    if not metadata.strip():
        return UnresolvedTargetFolder()
    try:
        decoded = cast(object, json.loads(metadata))
    except ValueError:
        return UnresolvedTargetFolder()
    if not isinstance(decoded, dict):
        return UnresolvedTargetFolder()
    fields = cast(dict[str, object], decoded)
    root, target = fields.get("workspace_root"), fields.get("target_directory")
    if (not isinstance(root, str) or not Path(root).is_absolute()
            or not isinstance(target, str) or not Path(target).is_absolute()):
        return UnresolvedTargetFolder()
    manifest = parse.manifest_path(argv)
    if manifest:
        selected = str((Path(cwd) / manifest).resolve())
        packages = fields.get("packages")
        manifests = [str(Path(root) / "Cargo.toml")]
        if isinstance(packages, list):
            manifests.extend(str(cast(dict[str, object], item).get("manifest_path", ""))
                             for item in cast(list[object], packages) if isinstance(item, dict))
        if selected not in manifests:
            return UnresolvedTargetFolder()
    elif not Path(cwd).is_relative_to(root):
        return UnresolvedTargetFolder()
    return CargoTargetFolder(Path(target))


def resolve_target_folder(argv: list[str], cwd: str, environment: Mapping[str, str], metadata: str = "") -> StepTargetFolder:
    """Resolve the folder from the command's environment, config, and manifests."""
    words, directory, effective = parse.command_context(argv, cwd, environment)
    if not words or Path(words[0]).name != "cargo" or parse.step_name(words) == "fmt":
        return NoTargetFolder()
    arguments = words[2:] if parse.toolchain(words) else words[1:]
    arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
    try:
        settings = cargo_folder_settings(arguments, directory, effective, policy=CargoConfigPolicy.CURRENT_STRICT)
        if chosen := settings.get("target-dir"):
            return CargoTargetFolder((chosen.base / chosen.value).resolve())
        variables = ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_HOME", "HOME")
        unchanged = directory == cwd and all(effective.get(name) == environment.get(name) for name in variables)
        if unchanged:
            known = effective.get("BUILDLOG_KNOWN_TARGET_DIR", "")
            if known and Path(known).is_absolute():
                return CargoTargetFolder(Path(known))
            supplied = metadata_target_folder(metadata, arguments, directory)
            if isinstance(supplied, CargoTargetFolder):
                return supplied
        workspace = cargo_workspace_manifest(arguments, directory, cargo_home_folder(directory, effective))
        return CargoTargetFolder(workspace.resolve().parent / "target")
    except (OSError, ValueError) as error:
        store.note_error(f"target folder: {error}")
        return UnresolvedTargetFolder()


def step_context(argv: list[str], metadata: str) -> None:
    if __package__:
        from . import treekey
    else:
        import treekey

    cwd = os.getcwd()
    print(treekey.tree_key(git_directory(argv, cwd)) or "")
    print(target_folder_text(resolve_target_folder(argv, cwd, os.environ, metadata)))


def git_directory(argv: list[str], cwd: str) -> str:
    """Find the manifest folder for steps that run outside the repository."""
    manifest = parse.manifest_path(argv) or os.environ.get("MANIFEST_PATH")
    if manifest:
        folder = (Path(cwd) / manifest).parent
        if folder.is_dir():
            return str(folder)
    return cwd


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "step-context":
        print("usage: step_context.py step-context ARGV...", file=sys.stderr)
        return 2
    step_context(argv[1:], sys.stdin.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
