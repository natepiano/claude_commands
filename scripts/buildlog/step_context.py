"""Capture a step's tree key and target folder without loading the writer."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable, cargo_metadata_output_roots, read_cargo_output_roots

from scripts.buildlog import parse


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
    import json

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


def cargo_command_context(argv: list[str], cwd: str, environment: Mapping[str, str]
                          ) -> tuple[list[str], list[str], str, Mapping[str, str]]:
    """Split Cargo inputs for start capture and output discovery."""
    words, directory, effective = parse.command_context(argv, cwd, environment)
    program = words[:2] if parse.toolchain(words) else words[:1]
    if not program or Path(program[0]).name != "cargo":
        return [], [], directory, effective
    arguments = words[len(program):]
    arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
    return program, arguments, directory, effective


def resolve_target_folder(argv: list[str], cwd: str, environment: Mapping[str, str], metadata: str = "") -> StepTargetFolder:
    """Resolve the folder from the command's environment, config, and manifests."""
    program, arguments, directory, effective = cargo_command_context(argv, cwd, environment)
    if not program or parse.step_name([*program, *arguments]) == "fmt":
        return NoTargetFolder()
    from scripts.lib.cargo_target_config import CargoConfigPolicy, cargo_folder_settings, cargo_home_folder, cargo_workspace_manifest

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
        from scripts.buildlog import store

        store.note_error(f"target folder: {error}")
        return UnresolvedTargetFolder()


_OUTPUT_ENVIRONMENT = (
    "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_BUILD_BUILD_DIR", "CARGO_HOME", "HOME", "RUSTUP_TOOLCHAIN",
)
_OUTPUT_ARGUMENTS = ("--manifest-path", "--config", "--target-dir")
_OUTPUT_FILES = ("Cargo.toml", "rust-toolchain", "rust-toolchain.toml")


def _output_arguments(arguments: list[str]) -> list[tuple[str, ...]]:
    """Keep the selected Cargo flags and values as written."""
    selected: list[tuple[str, ...]] = []
    words = iter(arguments)
    for word in words:
        if word in _OUTPUT_ARGUMENTS:
            selected.append((word, next(words, "")))
        elif word.startswith(tuple(flag + "=" for flag in _OUTPUT_ARGUMENTS)):
            selected.append((word,))
    return selected


def step_output_roots(argv: list[str], cwd: str) -> CargoOutputRoots | CargoOutputUnavailable:
    """Use matching exported folders or read the step's Cargo metadata."""
    import json
    if not TYPE_CHECKING:
        from scripts.lib.cargo_output import CargoOutputRoots, read_cargo_output_roots

    exported = os.environ.get("BUILDLOG_OUTPUT_ROOTS", "")
    if exported:
        roots = _parse_output_roots(exported, _output_evidence(argv, cwd, os.environ))
        if isinstance(roots, CargoOutputRoots):
            return roots
    program, arguments, directory, environment = cargo_command_context(argv, cwd, os.environ)
    options: list[str] = []
    target = ""
    for words in _output_arguments(arguments):
        if words[0].split("=", 1)[0] == "--target-dir":
            if not target:
                target = words[1] if len(words) == 2 else words[0].split("=", 1)[1]
        else:
            options.extend(words)
    if target:
        absolute = str((Path(directory) / target).resolve())
        options.extend(["--config", "build.target-dir=" + json.dumps(absolute)])
    return read_cargo_output_roots(Path(directory), cargo=program or ("cargo",),
                                   options=options, environment=environment)


def _output_evidence(argv: list[str], cwd: str, environment: Mapping[str, str]) -> str:
    """Fingerprint the Cargo inputs that select the workspace and output folders."""
    import hashlib
    import json
    from scripts.lib.cargo_target_config import cargo_config_folders

    program, arguments, directory, effective = cargo_command_context(argv, cwd, environment)
    if not program:
        return ""
    selected = [word for words in _output_arguments(arguments) for word in words]
    working = Path(directory).resolve()
    files = {folder / name for folder in (working, *working.parents) for name in _OUTPUT_FILES}
    files.update(folder / name for folder in cargo_config_folders(directory, effective)
                 for name in ("config", "config.toml"))
    contents: list[tuple[str, str, str]] = []
    try:
        for path in sorted(files):
            try:
                contents.append((str(path), "present", path.read_bytes().hex()))
            except FileNotFoundError:
                contents.append((str(path), "absent", ""))
    except OSError:
        return ""
    inputs = (str(working), program[1:], selected,
              [(name, effective.get(name)) for name in _OUTPUT_ENVIRONMENT], contents)
    return hashlib.sha256(json.dumps(inputs, separators=(",", ":")).encode()).hexdigest()


def _parse_output_roots(value: str, evidence: str) -> CargoOutputRoots | CargoOutputUnavailable:
    """Accept exported folders only when the Cargo evidence matches."""
    if not TYPE_CHECKING:
        from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable

    parts = value.split("\t")
    if len(parts) != 4 or not evidence or parts[0] != evidence:
        return CargoOutputUnavailable("output evidence does not match")
    return CargoOutputRoots(*(Path(part).resolve() for part in parts[1:]))


def _output_roots(metadata: str) -> None:
    if not TYPE_CHECKING:
        from scripts.lib.cargo_output import CargoOutputRoots, cargo_metadata_output_roots

    roots = cargo_metadata_output_roots(metadata)
    if isinstance(roots, CargoOutputRoots):
        evidence = _output_evidence(["cargo", "metadata"], os.getcwd(), os.environ)
        if evidence:
            print("\t".join((evidence, str(roots.workspace_root), str(roots.target_directory), str(roots.build_directory))))


def step_context(argv: list[str], metadata: str) -> None:
    from scripts.buildlog import treekey

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
    if argv == ["output-roots"]:
        _output_roots(sys.stdin.read())
        return 0
    if not argv or argv[0] != "step-context":
        print("usage: step_context.py {step-context ARGV...|output-roots}", file=sys.stderr)
        return 2
    step_context(argv[1:], sys.stdin.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
