"""Read the workspace and output roots that Cargo resolves for a checkout."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class CargoOutputRoots:
    workspace_root: Path
    target_directory: Path
    build_directory: Path


@dataclass(frozen=True)
class CargoOutputUnavailable:
    reason: str


def cargo_metadata_output_roots(text: str) -> CargoOutputRoots | CargoOutputUnavailable:
    """Decode the workspace and output folders in Cargo metadata."""
    try:
        metadata = cast(object, json.loads(text))
        if not isinstance(metadata, dict):
            return CargoOutputUnavailable("cargo metadata did not return an object")
        fields = cast(dict[str, object], metadata)
        workspace = fields.get("workspace_root")
        target = fields.get("target_directory")
        build = fields.get("build_directory", target)
        if not all(isinstance(value, str) and value.strip() for value in (workspace, target, build)):
            return CargoOutputUnavailable("cargo metadata has invalid output roots")
        paths = [Path(cast(str, value)) for value in (workspace, target, build)]
        if not all(path.is_absolute() for path in paths):
            return CargoOutputUnavailable("cargo metadata has relative output roots")
        return CargoOutputRoots(*(path.resolve() for path in paths))
    except (OSError, ValueError) as error:
        return CargoOutputUnavailable(str(error))


def read_cargo_output_roots(checkout: Path, *, cargo: Sequence[str] = ("cargo",), options: Sequence[str] = (),
                            environment: Mapping[str, str] | None = None
                            ) -> CargoOutputRoots | CargoOutputUnavailable:
    """Keep missing output directories in a valid Cargo mapping."""
    try:
        result = subprocess.run(
            [*cargo, "metadata", "--format-version", "1", "--no-deps", *options],
            cwd=checkout, capture_output=True, text=True, check=False,
            env=environment,
        )
        if result.returncode:
            return CargoOutputUnavailable(result.stderr.strip() or f"cargo metadata exited {result.returncode}")
        return cargo_metadata_output_roots(result.stdout)
    except (OSError, ValueError) as error:
        return CargoOutputUnavailable(str(error))
