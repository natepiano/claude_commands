#!/usr/bin/env python3
"""Find orphaned scripts and config files in .claude directory."""

import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict


class OrphanInfo(TypedDict):
    """Information about an orphaned file."""
    path: str
    category: str  # "script" or "config"


class ExpectedOrphans(TypedDict):
    """Expected orphan files that should be ignored."""
    scripts: list[str]
    config: list[str]


def find_scripts(scripts_dir: Path) -> list[Path]:
    """Find all bash and python scripts in the scripts directory."""
    scripts: list[Path] = []
    if scripts_dir.exists():
        scripts.extend(scripts_dir.rglob("*.sh"))
        scripts.extend(scripts_dir.rglob("*.py"))
    return scripts


def find_config_files(config_dir: Path) -> list[Path]:
    """Find all files in the config directory."""
    if config_dir.exists():
        return [f for f in config_dir.rglob("*") if f.is_file()]
    return []


def load_expected_orphans(claude_dir: Path) -> ExpectedOrphans:
    """Load the list of expected orphans from config."""
    config_file = claude_dir / "config" / "orphans_expected.json"

    if not config_file.exists():
        return {"scripts": [], "config": []}

    try:
        with open(config_file) as f:
            data: ExpectedOrphans = json.load(f)  # pyright: ignore[reportAny]
            return data
    except (OSError, json.JSONDecodeError):
        return {"scripts": [], "config": []}


def is_referenced(file_path: Path, claude_dir: Path) -> bool:
    """Check if a file is referenced anywhere in .claude directory."""
    file_name = file_path.name

    # Search in specific locations where references are likely
    search_paths = [
        str(claude_dir / "commands"),
        str(claude_dir / "shared"),
        str(claude_dir / "scripts"),
        str(claude_dir / "tests"),
        str(claude_dir / "CLAUDE.md"),
        str(claude_dir / "settings.json"),
    ]

    # Filter to only existing paths
    search_paths = [p for p in search_paths if Path(p).exists()]

    if not search_paths:
        return False

    # Use grep to search for references with timeout
    try:
        result = subprocess.run(
            [
                "grep",
                "-r",
                "-l",
                "-I",  # Skip binary files
                "-F",  # Fixed string search
                file_name,
            ] + search_paths,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,  # 5 second timeout
        )

        # Parse results to exclude self-references
        if result.returncode == 0:
            matching_files = result.stdout.strip().split("\n")
            for match in matching_files:
                match_path = Path(match)
                # If we found a reference in a different file, it's referenced
                if match_path != file_path and match_path.exists():
                    return True

        return False
    except subprocess.TimeoutExpired:
        print(f"Warning: Search timed out for {file_name}", file=sys.stderr)
        return True  # Conservative: assume referenced
    except (subprocess.SubprocessError, OSError):
        # If grep fails, conservatively assume it's referenced
        return True


def main() -> int:
    """Main entry point."""
    # Get .claude directory - either cwd if we're in it, or look for it
    cwd = Path.cwd()

    # Check if current directory is .claude
    if cwd.name == ".claude":
        claude_dir = cwd
    else:
        claude_dir = cwd / ".claude"

    if not claude_dir.exists():
        print("Error: .claude directory not found in current working directory", file=sys.stderr)
        return 1

    scripts_dir = claude_dir / "scripts"
    config_dir = claude_dir / "config"

    # Load expected orphans
    expected = load_expected_orphans(claude_dir)

    # Find all scripts and config files
    scripts = find_scripts(scripts_dir)
    config_files = find_config_files(config_dir)

    # Check for orphans
    orphans: list[OrphanInfo] = []

    for script in scripts:
        if not is_referenced(script, claude_dir):
            rel_path = str(script.relative_to(claude_dir))
            # Skip if this is an expected orphan
            if rel_path not in expected["scripts"]:
                orphans.append({"path": rel_path, "category": "script"})

    for config_file in config_files:
        if not is_referenced(config_file, claude_dir):
            rel_path = str(config_file.relative_to(claude_dir))
            # Skip if this is an expected orphan (and not the orphans_expected.json itself)
            if rel_path != "config/orphans_expected.json" and rel_path not in expected["config"]:
                orphans.append({"path": rel_path, "category": "config"})

    # Report results
    if not orphans:
        print("✓ No orphaned files found")
        return 0

    print(f"Found {len(orphans)} orphaned file(s):\n")

    # Group by category
    scripts_orphaned = [o for o in orphans if o["category"] == "script"]
    configs_orphaned = [o for o in orphans if o["category"] == "config"]

    if scripts_orphaned:
        print("Orphaned scripts:")
        for orphan in scripts_orphaned:
            print(f"  - {orphan['path']}")
        print()

    if configs_orphaned:
        print("Orphaned config files:")
        for orphan in configs_orphaned:
            print(f"  - {orphan['path']}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
