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
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from typing import TypedDict, cast
from urllib import parse, request
from urllib.error import HTTPError, URLError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bevy_upgrade"))

import crates_io_pacer


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
    kind: str


@dataclass(frozen=True)
class BevyRequirement:
    requirement: str


@dataclass(frozen=True)
class NoBevyDependency:
    pass


@dataclass(frozen=True)
class RequirementLookupFailed:
    reason: str


@dataclass(frozen=True)
class DiscoveredDependency:
    name: str
    version: str
    lookup: BevyRequirement | RequirementLookupFailed


@dataclass(frozen=True)
class CompatibleVersion:
    version: str


@dataclass(frozen=True)
class NoCompatibleVersion:
    pass


@dataclass(frozen=True)
class CompatibilityUnknown:
    reason: str


@dataclass(frozen=True)
class BevyCheckoutRead:
    names: frozenset[str]


@dataclass(frozen=True)
class BevyCheckoutMissing:
    path: Path


RequirementLookup = BevyRequirement | NoBevyDependency | RequirementLookupFailed
BevyCheckout = BevyCheckoutRead | BevyCheckoutMissing


def bevy_requirement_names(bevy_checkout: BevyCheckout) -> frozenset[str]:
    return bevy_checkout.names if isinstance(bevy_checkout, BevyCheckoutRead) else frozenset({'bevy'})


@dataclass
class DependencyInfo:
    """Information about a dependency"""
    name: str
    current_version: str
    latest_version: str
    bevy_compatible_version: str | None
    classification: str  # BLOCKER, UPDATE_REQUIRED, CHECK_NEEDED, OK
    reason: str


def get_bevy_internal_crates(bevy_version: str) -> BevyCheckout:
    """
    Get the list of bevy internal crates by parsing all Cargo.toml files
    in the cloned bevy repository (including nested macro crates).
    """
    bevy_repo = Path.home() / f"rust/bevy-{bevy_version}"
    crates_dir = bevy_repo / "crates"

    if not crates_dir.is_dir():
        return BevyCheckoutMissing(crates_dir)

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

    return BevyCheckoutRead(frozenset(internal_crates))


def get_workspace_members(codebase: Path) -> set[str]:
    """
    Get list of workspace member crate names using cargo metadata.
    Returns empty set if not a workspace or on error.
    """
    try:
        result = subprocess.run(
            ['cargo', 'metadata', '--no-deps', '--format-version', '1'],
            cwd=codebase,
            capture_output=True,
            text=True,
            check=True
        )

        data: dict[str, list[dict[str, str]]] = json.loads(result.stdout)  # pyright: ignore[reportAny]
        packages: list[dict[str, str]] = data.get('packages', [])  # type: ignore[assignment]

        # Extract package names from workspace members
        return {pkg['name'] for pkg in packages}

    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        # Not a workspace or error - return empty set
        return set()


def run_cargo_tree(codebase: Path) -> str | None:
    """
    Run `cargo tree` for direct dependencies and return its stdout.

    Cargo abbreviates already printed packages as `name vX (*)` without children,
    hiding a member's dependencies when an earlier member depends on it; --no-dedupe
    prints every package in full each time. --workspace includes every member even
    when the root manifest is also a package, which cargo otherwise lists alone.

    Tries `--all-features` first so optional / feature-gated dependencies are
    included; without it, deps behind a non-default feature are invisible to
    the tree. Falls back to the default feature set if `--all-features` fails
    (e.g. mutually exclusive features). Returns None on error.
    """
    base_cmd = ['cargo', 'tree', '--workspace', '--no-dedupe', '--depth=1', '--format', '{p}']

    for extra_args in (['--all-features'], []):
        try:
            result = subprocess.run(
                base_cmd + extra_args,
                cwd=codebase,
                capture_output=True,
                text=True,
                check=True
            )
            stdout: str = result.stdout
            return stdout
        except subprocess.CalledProcessError as e:
            if extra_args:
                # `--all-features` failed (likely conflicting features); retry
                # with the default feature set.
                continue
            # stderr is str because we use text=True - stdlib types as Any
            error_msg = e.stderr if isinstance(e.stderr, str) else str(e)  # pyright: ignore[reportAny]
            print(f"Error running cargo tree: {error_msg}", file=sys.stderr)
            return None
        except FileNotFoundError:
            print("Error: cargo not found. Make sure Rust is installed.", file=sys.stderr)
            return None

    return None


def get_all_direct_dependencies(
    codebase: Path, bevy_checkout: BevyCheckout
) -> list[tuple[str, str]]:
    """
    Run cargo tree to find all DIRECT dependencies (from Cargo.toml).
    Returns list of (crate_name, version) tuples, excluding bevy internal crates and workspace members.
    Uses --depth=1 for direct dependencies, --workspace for every member,
    --no-dedupe for their complete trees, and --all-features for optional dependencies.
    --no-dedupe repeats a shared crate under every member that uses it; the set
    returns each (crate_name, version) once, so discovery looks it up once.
    """
    # Get the definitive list of bevy internal crates
    internal_crates: frozenset[str] = bevy_checkout.names if isinstance(bevy_checkout, BevyCheckoutRead) else frozenset()

    # Get workspace members to exclude
    workspace_members = get_workspace_members(codebase)

    # Also exclude non-bevy ecosystem crates (standard utility crates)
    utility_crates = {
        'rand', 'serde', 'serde_json', 'tokio', 'async-std', 'futures',
        'log', 'tracing', 'anyhow', 'thiserror', 'clap', 'strum',
        'itertools', 'regex', 'chrono', 'uuid', 'parking_lot',
        'crossbeam', 'rayon', 'nalgebra', 'glam', 'bytemuck',
    }

    stdout = run_cargo_tree(codebase)
    if stdout is None:
        return []

    dependencies: set[tuple[str, str]] = set()

    for line in stdout.splitlines():
        # Extract crate name and version, handling tree characters (├──, │, etc.)
        match = re.search(r'([a-zA-Z0-9_-]+)\s+v(\d\S*)', line)
        if match:
            crate_name = match.group(1)
            version = match.group(2)

            # Exclude bevy internal crates, workspace members, and common utility crates
            if (crate_name not in internal_crates
                and crate_name not in workspace_members
                and crate_name not in utility_crates):
                dependencies.add((crate_name, version))

    return sorted(list(dependencies))


def get_bevy_dependencies(
    codebase: Path, bevy_checkout: BevyCheckout
) -> list[DiscoveredDependency]:
    """
    Find all DIRECT dependencies that depend on Bevy.
    Keeps each dependency's name, version and the lookup outcome that retained it.

    This function checks ALL direct dependencies (not just ones with 'bevy' in the name)
    against crates.io to determine if they depend on Bevy.
    """
    all_deps = get_all_direct_dependencies(codebase, bevy_checkout)
    bevy_names = bevy_requirement_names(bevy_checkout)

    bevy_deps: list[DiscoveredDependency] = []

    for crate_name, version in all_deps:
        print(f"  Checking if {crate_name} depends on bevy...", file=sys.stderr)
        match get_bevy_dependency_requirement(crate_name, version, bevy_names):
            case (BevyRequirement() | RequirementLookupFailed()) as lookup:
                bevy_deps.append(DiscoveredDependency(crate_name, version, lookup))
            case NoBevyDependency():
                pass

    return bevy_deps


def query_crates_io(crate_name: str) -> QueryResult | None:
    """
    Query crates.io API for crate information.
    Returns dict with version info or None on error.
    """
    url = f"https://crates.io/api/v1/crates/{crate_name}"

    try:
        req = request.Request(url)
        req.add_header('User-Agent', 'bevy-dependency-checker/1.0')

        crates_io_pacer.CRATES_IO_PACER.wait()
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


def get_bevy_dependency_requirement(
    crate_name: str, version: str, bevy_names: frozenset[str]
) -> RequirementLookup:
    """
    Query crates.io API for a specific version's bevy dependency requirement.
    Distinguishes a Bevy requirement, no Bevy dependency, and an unread release.
    """
    url = f"https://crates.io/api/v1/crates/{crate_name}/{parse.quote(version, safe='')}/dependencies"

    try:
        req = request.Request(url)
        req.add_header('User-Agent', 'bevy-dependency-checker/1.0')

        crates_io_pacer.CRATES_IO_PACER.wait()
        with cast(HTTPResponse, request.urlopen(req, timeout=10)) as response:
            raw = cast(object, json.loads(response.read().decode('utf-8')))
            if not isinstance(raw, dict):
                raise ValueError('expected a dependencies object')
            data = cast(dict[str, object], raw)
            values = data['dependencies']
            if not isinstance(values, list):
                raise ValueError('expected a dependencies array')
            dependencies: list[DependencyItem] = []
            for value in cast(list[object], values):
                if not isinstance(value, dict):
                    raise ValueError('expected a dependency object')
                fields = cast(dict[str, object], value)
                name, requirement, kind = fields['crate_id'], fields['req'], fields['kind']
                if not isinstance(name, str) or not isinstance(requirement, str) or not isinstance(kind, str):
                    raise ValueError('dependency crate_id, req and kind must be strings')
                if kind != 'dev' and (name == 'bevy' or name in bevy_names):
                    dependencies.append(DependencyItem(crate_id=name, req=requirement, kind=kind))

            if not dependencies:
                return NoBevyDependency()
            dep = min(dependencies, key=lambda dep: (dep['crate_id'] != 'bevy', dep['crate_id']))
            return BevyRequirement(dep['req'])

    except (OSError, HTTPException, ValueError, KeyError, TypeError) as e:
        reason = f"crates.io response is missing {e}" if isinstance(e, KeyError) else str(e)
        print(f"Warning: Could not query dependencies for {crate_name} {version}: {reason}", file=sys.stderr)
        return RequirementLookupFailed(reason)


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
    target_bevy_version: str,
    bevy_checkout: BevyCheckout
) -> CompatibleVersion | NoCompatibleVersion | CompatibilityUnknown:
    """
    Find the latest version compatible with target Bevy version by checking
    each version's actual bevy dependency requirement.
    Only checks the 20 most recent versions to avoid matching ancient versions.
    """
    # Only check the 20 most recent versions to avoid false matches from years ago
    recent_versions = all_versions[:20]
    bevy_names = bevy_requirement_names(bevy_checkout)
    unsuccessful: NoCompatibleVersion | CompatibilityUnknown = NoCompatibleVersion()

    # Check versions from newest to oldest
    for version in recent_versions:
        match get_bevy_dependency_requirement(crate_name, version, bevy_names):
            case BevyRequirement(requirement):
                if version_matches_requirement(requirement, target_bevy_version):
                    return CompatibleVersion(version)
            case RequirementLookupFailed(reason):
                if isinstance(unsuccessful, NoCompatibleVersion):
                    unsuccessful = CompatibilityUnknown(reason)
            case NoBevyDependency():
                pass

    return unsuccessful


def classify_dependency(
    current_version: str,
    latest_version: str,
    bevy_compatible_version: str | None,
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
    dependencies: list[DependencyInfo], warning: str = ''
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
    if warning:
        lines.extend([warning, ""])
    lines.append(f"**Status:** {len(dependencies)} dependencies checked")
    lines.append(f"- ✅ Compatible: {len(ok_deps)}")
    lines.append(f"- 🔄 Updates available: {len(updates_required)}")
    lines.append(f"- ⚠️  Needs verification: {len(check_needed)}")
    lines.append(f"- 🚫 Blockers: {len(blockers)}")
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
    _ = parser.add_argument('--output', type=Path, required=False, help='Output file path (default: stdout)')

    args = parser.parse_args()

    bevy_version = cast(str, args.bevy_version)
    codebase = cast(Path, args.codebase)
    output_path = cast(Path | None, args.output)

    # Defense in depth: Delete output file at start to prevent stale cached data
    if output_path and output_path.exists():
        try:
            output_path.unlink()
            print(f"Cleaned up stale output file: {output_path}", file=sys.stderr)
        except OSError as e:
            print(f"Warning: Could not delete stale output file {output_path}: {e}", file=sys.stderr)

    if not codebase.exists():
        print(f"Error: Codebase path does not exist: {codebase}", file=sys.stderr)
        sys.exit(1)

    # Get bevy dependencies from cargo tree
    print(f"Analyzing dependencies in {codebase}...", file=sys.stderr)
    bevy_checkout = get_bevy_internal_crates(bevy_version)
    warning = ''
    if isinstance(bevy_checkout, BevyCheckoutMissing):
        warning = (f"Warning: {bevy_checkout.path} not found; "
                   "crates that depend only on a Bevy sub-crate are not detected")
        print(warning, file=sys.stderr)
    deps = get_bevy_dependencies(codebase, bevy_checkout)

    if not deps:
        print("No bevy-related dependencies found.", file=sys.stderr)
        # Still output a minimal report
        report = "## ⚠️ Dependency Compatibility Review\n\n"
        if warning:
            report += warning + '\n\n'
        report += "**Status:** No bevy-related dependencies found in this project\n"

        # Output to file or stdout
        if output_path:
            _ = output_path.write_text(report)
            print(f"✓ Report written to {output_path}", file=sys.stderr)
        else:
            print(report)

        sys.exit(0)

    print(f"Found {len(deps)} bevy-related dependencies", file=sys.stderr)

    # Analyze each dependency
    dependency_infos: list[DependencyInfo] = []

    for dependency in deps:
        dep_name, current_version = dependency.name, dependency.version
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

        # Find compatible version
        compatibility = find_bevy_compatible_version(dep_name, all_versions, bevy_version, bevy_checkout)
        match compatibility, dependency.lookup:
            case CompatibleVersion(version), _:
                compatible_version = version
                classification, reason = classify_dependency(
                    current_version, latest_version, version, bevy_version
                )
            case CompatibilityUnknown(failure), _:
                compatible_version = None
                classification = 'CHECK_NEEDED'
                reason = ("Could not read every release's dependencies from crates.io "
                          f"({failure}) - manual verification needed")
            case NoCompatibleVersion(), BevyRequirement():
                compatible_version = None
                classification, reason = classify_dependency(
                    current_version, latest_version, None, bevy_version
                )
            case NoCompatibleVersion(), RequirementLookupFailed(failure):
                compatible_version = None
                classification = 'CHECK_NEEDED'
                reason = (f"Could not read release {current_version}'s dependencies from crates.io "
                          f"({failure}) - manual verification needed")

        dependency_infos.append(DependencyInfo(
            name=dep_name,
            current_version=current_version,
            latest_version=latest_version,
            bevy_compatible_version=compatible_version,
            classification=classification,
            reason=reason
        ))

    # Generate markdown report
    report = generate_markdown_report(dependency_infos, warning)

    # Output to file or stdout
    if output_path:
        _ = output_path.write_text(report)
        print(f"✓ Report written to {output_path}", file=sys.stderr)
    else:
        print(report)

    print(f"✓ Dependency check complete", file=sys.stderr)


if __name__ == '__main__':
    main()
