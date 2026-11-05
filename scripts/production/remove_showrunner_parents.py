"""Remove obsolete parent files from an explicit showrunner state directory."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast


def remove_parent_files(state_dir: Path) -> tuple[Path, ...]:
    """Remove parent files; repeated calls return no removed paths."""
    paths = tuple(sorted(state_dir.glob("*/parent")))
    for path in paths:
        path.unlink()
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    for path in remove_parent_files(cast(Path, args.state_dir)):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
