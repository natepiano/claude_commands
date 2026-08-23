#!/usr/bin/env python3
"""Resolve which cargo packages a lint run should cover.

Reads the cargo argv the caller is building, and prints the package-selection
flags for the tree that argv targets, one token per line, for `invoke.sh` to
splice into the command:

    --workspace          cover every member
    -p<newline>name      cover just these members (repeated)
    (nothing)            not a workspace; let cargo pick by cwd

The point is that a workspace-wide lint recompiles every member on every run,
and in a workspace whose members share one heavy dependency tree that cost is
paid over and over for crates the edit never touched. Scoping to the members
that actually changed removes it.

Scoping is deliberately conservative -- it widens to the whole workspace
whenever it cannot prove a narrow scope is safe:

  * a workspace-level file changed (root manifest, lockfile, or any shared
    tool config), so every member's build could differ
  * nothing changed at all, so there is no signal to narrow by
  * more than half the members changed, where a long -p list buys nothing
  * cargo metadata could not be read

Callers that must never narrow (the pre-push gate) pass --workspace
themselves; the lint CLI skips this resolver entirely when the caller already
chose a *package* scope -- --workspace or -p.

--manifest-path is not such a scope, and the distinction is the whole reason
this resolver takes arguments. It chooses a manifest, not a package set:
aimed at a virtual workspace root it leaves cargo's default selection at
"every member", so a per-project driver that passes it (cargo-port,
clean-fix) gets a workspace-wide compile with no --workspace anywhere in the
argv to show for it. Treating it as a caller-chosen scope therefore disabled
narrowing for every such driver. It is forwarded here instead, and metadata
and git status resolve against that manifest rather than the working
directory.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final, TypedDict

# Files whose contents can change how any member builds. A change to one of
# these forces the whole workspace regardless of which member it sits next to.
WORKSPACE_FILES: Final = frozenset(
    {
        "Cargo.toml",
        "Cargo.lock",
        "rustfmt.toml",
        ".rustfmt.toml",
        "clippy.toml",
        ".clippy.toml",
        "taplo.toml",
        ".taplo.toml",
        "rust-toolchain",
        "rust-toolchain.toml",
    }
)

# Directories at the workspace root with the same reach.
WORKSPACE_DIRS: Final = (".cargo", ".config")


class CargoPackage(TypedDict):
    """One entry of the `packages` array in `cargo metadata --no-deps`."""

    name: str
    manifest_path: str


class CargoMetadata(TypedDict):
    """The subset of `cargo metadata --no-deps` output this script reads."""

    workspace_root: str
    packages: list[CargoPackage]


def run(args: list[str], cwd: Path | None = None) -> str | None:
    """Run a command, returning its stdout, or None if it failed."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=60, cwd=cwd
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


def changed_paths(cwd: Path) -> list[Path] | None:
    """Absolute paths of every file git reports as changed or untracked.

    `cwd` is the workspace root, not the caller's working directory: with
    --manifest-path the two differ, and asking git about the wrong tree is how
    a narrow scope stops being safe. The repository root can still sit above
    the workspace root, so it is resolved rather than assumed.
    """
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    if top is None:
        return None
    root = Path(top.strip())

    out = run(["git", "status", "--porcelain", "-z"], cwd=cwd)
    if out is None:
        return None

    paths: list[Path] = []
    for entry in out.split("\0"):
        if len(entry) < 4:
            continue
        # Porcelain v1: two status columns, a space, then the path. A rename
        # reads `R  new -> old` across two NUL-separated fields; the first
        # field carries the new path, which is the one that matters here.
        paths.append(root / entry[3:])
    return paths


def resolve(manifest_path: Path | None) -> list[str]:
    """Work out the scope flags for the tree the caller's argv targets."""
    metadata = load_metadata(manifest_path)
    if metadata is None:
        return ["--workspace"]

    packages = metadata["packages"]
    if len(packages) <= 1:
        # A single-crate project has nothing to narrow to; cargo's own default
        # already covers it, so add no flags at all.
        return []

    workspace_root = Path(metadata["workspace_root"]).resolve()

    # Longest path first, so a nested member wins over its parent.
    members: list[tuple[Path, str]] = sorted(
        (
            (Path(pkg["manifest_path"]).resolve().parent, pkg["name"])
            for pkg in packages
        ),
        key=lambda item: len(item[0].parts),
        reverse=True,
    )

    changed = changed_paths(workspace_root)
    if changed is None or not changed:
        return ["--workspace"]

    selected: set[str] = set()
    for path in changed:
        resolved = path.resolve()

        try:
            relative = resolved.relative_to(workspace_root)
        except ValueError:
            # Outside the workspace entirely; it cannot identify a member, and
            # it might be a sibling crate this one path-depends on.
            return ["--workspace"]

        if len(relative.parts) == 1 and relative.name in WORKSPACE_FILES:
            return ["--workspace"]
        if relative.parts and relative.parts[0] in WORKSPACE_DIRS:
            return ["--workspace"]

        owner = next(
            (
                name
                for directory, name in members
                if resolved == directory or directory in resolved.parents
            ),
            None,
        )
        if owner is None:
            # Inside the workspace but owned by no member -- a top-level
            # docs/, scripts/, or CI file. Nothing to compile, so ignore it
            # rather than widening the scope over a README edit.
            continue
        selected.add(owner)

    if not selected:
        return ["--workspace"]
    if len(selected) * 2 > len(packages):
        return ["--workspace"]

    flags: list[str] = []
    for name in sorted(selected):
        flags.extend(("-p", name))
    return flags


def main() -> int:
    for token in resolve(manifest_path_of(sys.argv[1:])):
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
