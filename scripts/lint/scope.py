#!/usr/bin/env python3
"""Resolve which cargo packages a lint run should cover.

Reads the cargo argv the caller is building, and prints the package-selection
flags for the tree that argv targets, one token per line, for `invoke.sh` to
splice into the command:

    --workspace          a workspace with more than one member
    (nothing)            a single-crate project; let cargo pick by cwd

This used to narrow to the members the working tree changed (`-p name`
repeated), so that a workspace-wide lint would not recompile members an edit
never touched. The narrowing cost more than it saved. Cargo resolves features
per invocation from the selected packages, so each distinct `-p` set gave the
shared dependencies (bevy and the rest) their own feature set and their own
compiled copy in the target directory. In hana that was nine to eleven copies
of bevy_render across the dev loop's selections, evicting one another under
the sweep budget and rebuilding in minutes each. Under --workspace an
untouched member is a fingerprint check, and every run shares one copy.

Callers that already chose a *package* scope -- --workspace or -p -- skip
this resolver; the lint CLI checks for them first.

--manifest-path is not such a scope. It chooses a manifest, not a package
set, so it is forwarded here, and metadata resolves against that manifest
rather than the working directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict


class CargoPackage(TypedDict):
    """One entry of the `packages` array in `cargo metadata --no-deps`."""

    name: str


class CargoMetadata(TypedDict):
    """The subset of `cargo metadata --no-deps` output this script reads."""

    packages: list[CargoPackage]


def run(args: list[str]) -> str | None:
    """Run a command, returning its stdout, or None if it failed."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def manifest_path_of(argv: list[str]) -> Path | None:
    """Pull `--manifest-path` out of the cargo argv the caller forwarded.

    Both spellings cargo accepts are handled. A trailing `--manifest-path`
    with nothing after it is malformed; cargo will reject it, so this reports
    no manifest and lets the resolution fall back to the working directory.
    """
    for index, token in enumerate(argv):
        if token == "--manifest-path":
            following = argv[index + 1 : index + 2]
            return Path(following[0]) if following else None
        if token.startswith("--manifest-path="):
            return Path(token.split("=", 1)[1])
    return None


def load_metadata(manifest_path: Path | None) -> CargoMetadata | None:
    """Read workspace members. --no-deps skips resolution, so this is cheap."""
    args = ["cargo", "metadata", "--no-deps", "--format-version", "1"]
    if manifest_path is not None:
        args.extend(("--manifest-path", str(manifest_path)))
    out = run(args)
    if out is None:
        return None
    try:
        data: CargoMetadata = json.loads(out)  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        return None
    return data


def resolve(manifest_path: Path | None) -> list[str]:
    """Work out the scope flags for the tree the caller's argv targets."""
    metadata = load_metadata(manifest_path)
    if metadata is None:
        return ["--workspace"]
    if len(metadata["packages"]) <= 1:
        # A single-crate project has nothing to select between; cargo's own
        # default already covers it, so add no flags at all.
        return []
    return ["--workspace"]


def main() -> int:
    for token in resolve(manifest_path_of(sys.argv[1:])):
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
