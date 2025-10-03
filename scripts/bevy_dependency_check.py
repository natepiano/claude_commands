#!/usr/bin/env python3
"""
Bevy Dependency Compatibility Checker

Analyzes a Bevy project's dependencies to determine compatibility with a target Bevy version.

Usage: bevy_dependency_check.py --bevy-version <version> --codebase <path>
Example: bevy_dependency_check.py --bevy-version 0.17.1 --codebase ~/rust/my_game

Exit codes: 0 = success, 1 = error
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast
from urllib import request
from urllib.error import HTTPError, URLError


class CrateVersion(TypedDict):
    """Type for crate version from crates.io API"""
    num: str
    yanked: bool


class CrateData(TypedDict, total=False):
    """Type for crate data from crates.io API"""
    max_version: str
    updated_at: str


class CratesIOResponse(TypedDict, total=False):
    """Type for crates.io API response"""
    crate: CrateData
    versions: list[CrateVersion]


class QueryResult(TypedDict):
    """Return type for query_crates_io"""
    latest_version: str
    all_versions: list[str]
    updated_at: str


class DependencyItem(TypedDict):
    """Type for dependency item from crates.io API"""
    crate_id: str
    req: str


@dataclass
class DependencyInfo:
    """Information about a dependency"""
    name: str
    current_version: str
    latest_version: str
    bevy_compatible_version: str | None
    classification: str  # BLOCKER, UPDATE_REQUIRED, CHECK_NEEDED, OK
    reason: str


def get_bevy_internal_crates(bevy_version: str) -> set[str]:
    """
    Get the list of bevy internal crates by parsing all Cargo.toml files
    in the cloned bevy repository (including nested macro crates).
    """
    bevy_repo = Path.home() / f"rust/bevy-{bevy_version}"
    crates_dir = bevy_repo / "crates"

    if not crates_dir.exists():
        # Fallback: return empty set, we'll just include everything
        return set()

    internal_crates: set[str] = {'bevy', 'bevy_internal'}

    # Find all Cargo.toml files recursively in crates/
    for cargo_toml in crates_dir.rglob('Cargo.toml'):
        try:
            content = cargo_toml.read_text()
            # Extract package name using regex
            match = re.search(r'^\s*name\s*=\s*"([^"]+)"', content, re.MULTILINE)
            if match:
                internal_crates.add(match.group(1))
        except (OSError, UnicodeDecodeError):
            # Skip files we can't read
            continue

    return internal_crates


def get_bevy_dependencies(codebase: Path, bevy_version: str) -> list[tuple[str, str]]:
    """
    Run cargo tree to find all DIRECT dependencies (from Cargo.toml) that depend on bevy.
    Returns list of (crate_name, version) tuples, excluding bevy internal crates.
    Uses --depth=1 to only get direct dependencies, not transitive ones.
    """
    # Get the definitive list of bevy internal crates
    internal_crates = get_bevy_internal_crates(bevy_version)

    try:
        # Use --depth=1 to only get direct dependencies
        result = subprocess.run(
            ['cargo', 'tree', '--depth=1', '--format', '{p}'],
            cwd=codebase,
            capture_output=True,
            text=True,
            check=True
        )

        dependencies: set[tuple[str, str]] = set()

        stdout: str = result.stdout
        for line in stdout.splitlines():
            # Match pattern like "bevy_egui v0.29.0" or "bevy v0.16.0"
            if 'bevy' in line.lower():
                # Extract crate name and version, handling tree characters (├──, │, etc.)
                match = re.search(r'([a-zA-Z0-9_-]+)\s+v([0-9.]+)', line)
                if match:
                    crate_name = match.group(1)
                    version = match.group(2)

                    # Only include third-party bevy ecosystem crates (not bevy internal crates)
                    if crate_name not in internal_crates:
                        dependencies.add((crate_name, version))

        return sorted(list(dependencies))

    except subprocess.CalledProcessError as e:
        # stderr is str because we use text=True - stdlib types as Any
        error_msg = e.stderr if isinstance(e.stderr, str) else str(e)  # pyright: ignore[reportAny]
        print(f"Error running cargo tree: {error_msg}", file=sys.stderr)
        return []
    except FileNotFoundError:
        print("Error: cargo not found. Make sure Rust is installed.", file=sys.stderr)
        return []


def query_crates_io(crate_name: str) -> QueryResult | None:
    """
    Query crates.io API for crate information.
    Returns dict with version info or None on error.
    """
    url = f"https://crates.io/api/v1/crates/{crate_name}"

    try:
        req = request.Request(url)
        req.add_header('User-Agent', 'bevy-dependency-checker/1.0')

        with request.urlopen(req, timeout=10) as response:  # pyright: ignore[reportAny]
            data: CratesIOResponse = json.loads(response.read().decode('utf-8'))  # pyright: ignore[reportAny]

            if 'crate' not in data:
                return None

            crate_data: CrateData = data['crate']
            versions: list[CrateVersion] = data.get('versions', [])  # type: ignore[assignment]

            # Get latest version
            latest_version: str = crate_data.get('max_version', 'unknown')  # type: ignore[assignment]

            # Get all versions
            version_list: list[str] = [v['num'] for v in versions if not v.get('yanked', False)]

            return {
                'latest_version': latest_version,
                'all_versions': version_list,
                'updated_at': crate_data.get('updated_at', ''),
            }

    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"Warning: Could not query crates.io for {crate_name}: {e}", file=sys.stderr)
        return None


def get_bevy_dependency_requirement(crate_name: str, version: str) -> str | None:
    """
    Query crates.io API for a specific version's bevy dependency requirement.
    Returns the version requirement string (e.g., "0.17") or None if no bevy dependency.
    """
    url = f"https://crates.io/api/v1/crates/{crate_name}/{version}/dependencies"

    try:
        req = request.Request(url)
        req.add_header('User-Agent', 'bevy-dependency-checker/1.0')

        with request.urlopen(req, timeout=10) as response:  # pyright: ignore[reportAny]
            data: dict[str, list[DependencyItem]] = json.loads(response.read().decode('utf-8'))  # pyright: ignore[reportAny]

            dependencies: list[DependencyItem] = data.get('dependencies', [])  # type: ignore[assignment]

            # Find bevy dependency
            for dep in dependencies:
                if dep['crate_id'] == 'bevy':
                    return dep['req']

            return None

    except (HTTPError, URLError, json.JSONDecodeError) as e:
        print(f"Warning: Could not query dependencies for {crate_name} {version}: {e}", file=sys.stderr)
        return None


def version_matches_requirement(requirement: str, target_version: str) -> bool:
    """
    Check if a target version matches a cargo version requirement.
    Handles caret (^), tilde (~), exact (=), and bare version requirements.

    Examples:
      ^0.17.0 matches 0.17.1 (same major.minor)
      ~0.17.0 matches 0.17.1 (same major.minor)
      0.17 matches 0.17.1 (same major.minor prefix)
      =0.17.1 matches 0.17.1 only (exact)
    """
    req_stripped = requirement.strip()

    # Handle exact version (=0.17.1)
    if req_stripped.startswith('='):
        return req_stripped[1:].strip() == target_version

    # Remove caret ^ or tilde ~ operators
    req_clean = req_stripped.lstrip('^~')

    # Parse version parts
    target_parts = target_version.split('.')
    req_parts = req_clean.split('.')

    # Match on major.minor - this handles ^0.17.0 matching 0.17.1
    if len(target_parts) >= 2 and len(req_parts) >= 2:
        return target_parts[0] == req_parts[0] and target_parts[1] == req_parts[1]

    # Fallback: check prefix match for "0.17" style requirements
    if target_version.startswith(req_clean):
        return True

    return False


def find_bevy_compatible_version(
    crate_name: str,
    all_versions: list[str],
    target_bevy_version: str
) -> str | None:
    """
    Find the latest version compatible with target Bevy version by checking
    each version's actual bevy dependency requirement.
    Only checks the 20 most recent versions to avoid matching ancient versions.
    """
    # Only check the 20 most recent versions to avoid false matches from years ago
    recent_versions = all_versions[:20]

    # Check versions from newest to oldest
    for version in recent_versions:
        bevy_req = get_bevy_dependency_requirement(crate_name, version)

        if bevy_req and version_matches_requirement(bevy_req, target_bevy_version):
            return version

    return None


def classify_dependency(
    _dep_name: str,
    current_version: str,
    latest_version: str,
    bevy_compatible_version: str | None,
    _updated_at: str,
    target_bevy_version: str
) -> tuple[str, str]:
    """
    Classify dependency compatibility based on actual bevy dependency checks.
    Returns (classification, reason) tuple.
    """
    # No compatible version exists - blocker
    if bevy_compatible_version is None:
        return (
            'BLOCKER',
            (f'No version found compatible with Bevy {target_bevy_version}. '
             f'Latest version: {latest_version}')
        )

    # Current version is compatible
    if current_version == bevy_compatible_version:
        return (
            'OK',
            f'Compatible with Bevy {target_bevy_version}'
        )

    # Compatible version exists but we're not using it
    return (
        'UPDATE_REQUIRED',
        (f'Current version {current_version} incompatible. '
         f'Update to {bevy_compatible_version} for Bevy {target_bevy_version} support')
    )


def generate_markdown_report(
    dependencies: list[DependencyInfo],
    bevy_version: str,
    _codebase: Path
) -> str:
    """Generate markdown report of dependency compatibility."""

    # Group by classification
    blockers = [d for d in dependencies if d.classification == 'BLOCKER']
    updates_required = [d for d in dependencies if d.classification == 'UPDATE_REQUIRED']
    check_needed = [d for d in dependencies if d.classification == 'CHECK_NEEDED']
    ok_deps = [d for d in dependencies if d.classification == 'OK']

    lines: list[str] = []

    lines.append("## ⚠️ Dependency Compatibility Review")
    lines.append("")
    lines.append(f"**Status:** {len(dependencies)} dependencies checked")
    lines.append(f"- ✅ Compatible: {len(ok_deps)}")
    lines.append(f"- 🔄 Updates available: {len(updates_required)}")
    lines.append(f"- ⚠️  Needs verification: {len(check_needed)}")
    lines.append(f"- 🚫 Blockers: {len(blockers)}")
    lines.append("")

    # Add explanation of classifications
    lines.append("### Classification Guide")
    lines.append("")
    lines.append(f"**🚫 BLOCKER**: No version exists that's compatible with Bevy {bevy_version}. Cannot migrate until resolved.")
    lines.append("")
    lines.append("**🔄 UPDATE_REQUIRED**: Current version is incompatible, but a newer compatible version exists. Must update Cargo.toml.")
    lines.append("")
    lines.append("**⚠️ CHECK_NEEDED**: Compatibility unclear - needs manual verification through testing or checking maintainer updates.")
    lines.append("")
    lines.append(f"**✅ OK**: Already compatible with Bevy {bevy_version}.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Blockers section
    if blockers:
        lines.append("### 🚫 Blockers (Must Resolve Before Migration)")
        lines.append("")
        for dep in blockers:
            lines.append(f"- **`{dep.name} = \"{dep.current_version}\"`** - {dep.reason}")
            lines.append(f"  - Latest version: {dep.latest_version}")
            lines.append(f"  - Action: Wait for maintainer update or find alternative")
        lines.append("")

    # Updates required section
    if updates_required:
        lines.append("### 🔄 Updates Required")
        lines.append("")
        for dep in updates_required:
            target_version = dep.bevy_compatible_version or dep.latest_version
            lines.append(f"- **`{dep.name} = \"{dep.current_version}\"`** → `\"{target_version}\"`")
            lines.append(f"  - {dep.reason}")
            lines.append(f"  - Action: Update in Cargo.toml to version {target_version}")
        lines.append("")

    # Check needed section
    if check_needed:
        lines.append("### ⚠️ Verification Needed")
        lines.append("")
        for dep in check_needed:
            lines.append(f"- **`{dep.name} = \"{dep.current_version}\"`**")
            lines.append(f"  - {dep.reason}")
            lines.append(f"  - Latest version: {dep.latest_version}")
            lines.append(f"  - Action: Test compatibility or check maintainer status")
        lines.append("")

    # OK section
    if ok_deps:
        lines.append("### ✅ Compatible Dependencies")
        lines.append("")
        for dep in ok_deps:
            lines.append(f"- `{dep.name} = \"{dep.current_version}\"` - {dep.reason}")
        lines.append("")

    # Recommended actions
    lines.append("### Recommended Actions")
    lines.append("")
    if blockers:
        lines.append("1. **🚫 Address blockers first** - Migration cannot proceed without resolving these")
    if updates_required:
        lines.append(f"{'2' if blockers else '1'}. **🔄 Update dependencies** - Bump versions in Cargo.toml")
    if check_needed:
        next_num = 1 + len([x for x in [blockers, updates_required] if x])
        lines.append(f"{next_num}. **⚠️ Test unverified dependencies** - Run `cargo check` after updating Bevy")
    lines.append(f"{len([x for x in [blockers, updates_required, check_needed] if x]) + 1}. **Monitor for updates** - Check dependency issue trackers if blockers exist")
    lines.append("")

    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Check Bevy dependency compatibility'
    )
    _ = parser.add_argument('--bevy-version', required=True, help='Target Bevy version (e.g., 0.17.1)')
    _ = parser.add_argument('--codebase', type=Path, required=True, help='Path to Bevy project')

    args = parser.parse_args()

    bevy_version = cast(str, args.bevy_version)
    codebase = cast(Path, args.codebase)

    if not codebase.exists():
        print(f"Error: Codebase path does not exist: {codebase}", file=sys.stderr)
        sys.exit(1)

    # Get bevy dependencies from cargo tree
    print(f"Analyzing dependencies in {codebase}...", file=sys.stderr)
    deps = get_bevy_dependencies(codebase, bevy_version)

    if not deps:
        print("No bevy-related dependencies found.", file=sys.stderr)
        # Still output a minimal report
        print("\n## ⚠️ Dependency Compatibility Review")
        print("")
        print("**Status:** No bevy-related dependencies found in this project")
        print("")
        sys.exit(0)

    print(f"Found {len(deps)} bevy-related dependencies", file=sys.stderr)

    # Analyze each dependency
    dependency_infos: list[DependencyInfo] = []

    for dep_name, current_version in deps:
        print(f"Checking {dep_name}...", file=sys.stderr)

        # Query crates.io
        crate_info = query_crates_io(dep_name)

        if not crate_info:
            # Could not query - mark as CHECK_NEEDED
            dependency_infos.append(DependencyInfo(
                name=dep_name,
                current_version=current_version,
                latest_version='unknown',
                bevy_compatible_version=None,
                classification='CHECK_NEEDED',
                reason='Could not query crates.io - manual verification needed'
            ))
            continue

        latest_version = crate_info['latest_version']
        all_versions = crate_info['all_versions']
        updated_at = crate_info['updated_at']

        # Find compatible version
        compatible_version = find_bevy_compatible_version(dep_name, all_versions, bevy_version)

        # Classify
        classification, reason = classify_dependency(
            dep_name,
            current_version,
            latest_version,
            compatible_version,
            updated_at,
            bevy_version
        )

        dependency_infos.append(DependencyInfo(
            name=dep_name,
            current_version=current_version,
            latest_version=latest_version,
            bevy_compatible_version=compatible_version,
            classification=classification,
            reason=reason
        ))

    # Generate markdown report
    report = generate_markdown_report(dependency_infos, bevy_version, codebase)

    # Output to stdout
    print(report)

    print(f"✓ Dependency check complete", file=sys.stderr)


if __name__ == '__main__':
    main()
