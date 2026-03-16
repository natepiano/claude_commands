#!/usr/bin/env python3
"""
Get migration guide tranche for a specific subagent.

Returns JSON with guide files assigned to this subagent based on index.
Divides 114+ guides evenly across 10 subagents.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import TypedDict


class TranchResult(TypedDict):
    """Result of tranche calculation."""
    subagent_index: int
    total_guides: int
    assigned_guides: list[str]
    guide_count: int
    guides_dir: str


def get_tranche(guides_dir: Path, subagent_index: int, total_subagents: int = 10) -> TranchResult:
    """
    Get the tranche of migration guide files for a specific subagent.

    Args:
        guides_dir: Path to migration-guides directory
        subagent_index: 1-based index (1-10)
        total_subagents: Total number of subagents (default 10)

    Returns:
        dict with:
            - subagent_index: The index provided
            - total_guides: Total number of guides found
            - assigned_guides: List of guide file paths
            - guide_count: Number of guides assigned to this subagent
    """
    # Validate index
    if not 1 <= subagent_index <= total_subagents:
        raise ValueError(f"subagent_index must be between 1 and {total_subagents}")

    # Find all migration guide markdown files
    guides = sorted(guides_dir.glob("*.md"))

    if not guides:
        raise ValueError(f"No migration guides found in {guides_dir}")

    total_guides = len(guides)

    # Distribute guides evenly
    # With 114 guides and 10 subagents:
    # - base_count = 11 (114 // 10)
    # - remainder = 4 (114 % 10)
    # - First 4 subagents get 12 guides (11 + 1)
    # - Last 6 subagents get 11 guides

    base_count = total_guides // total_subagents
    remainder = total_guides % total_subagents

    # Calculate start and end indices for this subagent
    if subagent_index <= remainder:
        # This subagent gets base_count + 1 guides
        guides_for_this = base_count + 1
        start_idx = (subagent_index - 1) * guides_for_this
    else:
        # This subagent gets base_count guides
        guides_for_this = base_count
        start_idx = remainder * (base_count + 1) + (subagent_index - remainder - 1) * base_count

    end_idx = start_idx + guides_for_this

    # Get assigned guides for this subagent
    assigned = guides[start_idx:end_idx]

    return {
        "subagent_index": subagent_index,
        "total_guides": total_guides,
        "assigned_guides": [str(g.relative_to(guides_dir.parent.parent)) for g in assigned],
        "guide_count": len(assigned),
        "guides_dir": str(guides_dir)
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Get migration guide tranche for a specific subagent"
    )
    _ = parser.add_argument(
        "--guides-dir",
        required=True,
        help="Path to migration-guides directory (e.g., ~/rust/bevy-0.17.1/release-content/migration-guides)"
    )
    _ = parser.add_argument(
        "--subagent-index",
        type=int,
        required=True,
        help="Subagent index (1-10)"
    )
    _ = parser.add_argument(
        "--total-subagents",
        type=int,
        default=10,
        help="Total number of subagents (default: 10)"
    )

    args = parser.parse_args()

    guides_dir_arg: str = str(args.guides_dir)  # pyright: ignore[reportAny]
    guides_dir = Path(guides_dir_arg).expanduser()

    if not guides_dir.exists():
        print(f"Error: Guides directory does not exist: {guides_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        subagent_idx: int = int(args.subagent_index)  # pyright: ignore[reportAny]
        total: int = int(args.total_subagents)  # pyright: ignore[reportAny]
        result = get_tranche(guides_dir, subagent_idx, total)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
