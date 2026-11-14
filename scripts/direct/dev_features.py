"""Print verify.sh's setup lines: dev features, then the test environment policy.

Usage: dev_features.py <Cargo.toml> <verify command>

Line 1 is `workspace.metadata.dev.features` joined with commas. A `test` or
`final` command adds three lines from the result environment policy: the
forwarded names, the prefixes, and the fixed NAME=value pairs. verify.sh runs
this in its per-run helper, so it starts no Python process of its own.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import cast


def dev_features(manifest: Path) -> list[str]:
    """Read the dev feature names; a missing manifest has none."""
    try:
        with manifest.open("rb") as source:
            value: object = tomllib.load(source)
    except FileNotFoundError:
        return []
    for key in ("workspace", "metadata", "dev"):
        if not isinstance(value, dict):
            raise ValueError("expected a table")
        value = cast(dict[str, object], value).get(key, {})
    if not isinstance(value, dict):
        raise ValueError("expected a table")
    features = cast(dict[str, object], value).get("features", [])
    if not isinstance(features, list):
        raise ValueError("expected a list of strings")
    names: list[str] = []
    for name in cast(list[object], features):
        if not isinstance(name, str):
            raise ValueError("expected a list of strings")
        names.append(name)
    return names


def main(argv: list[str]) -> int:
    manifest, command = Path(argv[0]), argv[1] if len(argv) > 1 else ""
    try:
        features = dev_features(manifest)
    except (OSError, ValueError):
        print("verify.sh: workspace.metadata.dev.features must be a list of strings", file=sys.stderr)
        return 2
    print(",".join(features))
    if command in ("test", "final"):
        from scripts.direct.result_environment import read_policy

        policy = read_policy()
        print(" ".join(policy.forwarded_names))
        print(" ".join(policy.prefixes))
        print(" ".join(name + "=" + value for name, value in policy.fixed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
