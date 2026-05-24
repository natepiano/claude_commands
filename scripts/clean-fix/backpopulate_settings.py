#!/usr/bin/env python3
"""Back-populate canonical settings.local.json permissions into existing Rust projects.

For each project in ~/rust/:
1. Load canonical template permissions
2. Load existing project permissions (if any)
3. Remove entries subsumed by canonical wildcards
4. Add missing canonical entries
5. Keep project-specific entries
6. Write back (or create from scratch)

Dry-run by default. Pass --apply to write changes.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HOME: Path = Path.home()
TEMPLATE: Path = HOME / ".claude" / "templates" / "settings_local.json"
RUST_DIR: Path = HOME / "rust"

SCRIPT_PATTERN = (
    r"^Bash\((bash\s+)?"
    r"(~?/[^\s)]+\.(?:sh|py))"
    r"([\s:].*)?\)$"
)
SCRIPT_RE = re.compile(SCRIPT_PATTERN)

ABS_SCRIPT_PATTERN = (
    r"^Bash\((bash\s+)?"
    r"(/Users/\w+/(?:\.claude|rust)/[^\s)]+\.(?:sh|py))"
    r"([\s:].*)?\)$"
)
ABS_SCRIPT_RE = re.compile(ABS_SCRIPT_PATTERN)

OLD_VALIDATE_PATTERN = (
    r"^Bash\((bash\s+)?"
    r"(~?/[^\s)]*validate_ci\.sh|\.github/scripts/validate_ci\.sh)"
    r"([\s:].*)?\)$"
)
OLD_VALIDATE_RE = re.compile(OLD_VALIDATE_PATTERN)


@dataclass
class Report:
    project: str
    action: str  # "create" or "update"
    added: int = 0
    removed_subsumed: list[str] = field(default_factory=list)
    removed_old_paths: list[str] = field(default_factory=list)
    removed_junk: list[tuple[str, str]] = field(default_factory=list)
    added_canonical: list[str] = field(default_factory=list)
    kept_project_specific: int = 0
    total_before: int = 0
    total_after: int = 0


def load_json(path: Path) -> dict[str, object]:
    text = path.read_text()
    parsed: object = json.loads(text)  # pyright: ignore[reportAny]
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, object] = {}
    for k, v in parsed.items():  # pyright: ignore[reportUnknownVariableType]
        result[str(k)] = v  # pyright: ignore[reportUnknownArgumentType]
    return result


def save_json(path: Path, data: dict[str, object]) -> None:
    _ = path.write_text(json.dumps(data, indent=2) + "\n")


def normalize_script_path(path: str) -> str:
    """Normalize a script path: absolute home prefix -> ~/, strip leading ./"""
    home_str = str(HOME)
    if path.startswith(home_str):
        path = "~" + path[len(home_str):]
    if path.startswith("./"):
        path = path[2:]
    return path


def extract_script_base(perm: str) -> str | None:
    """Extract the normalized script path from a Bash permission."""
    for pattern in (SCRIPT_RE, ABS_SCRIPT_RE):
        m = pattern.match(perm)
        if m:
            return normalize_script_path(m.group(2))
    return None


def build_canonical_map(canonical_perms: list[str]) -> dict[str, list[str]]:
    """Map from normalized script path -> canonical wildcard entries covering it."""
    result: dict[str, list[str]] = {}
    for perm in canonical_perms:
        script = extract_script_base(perm)
        if script and (perm.endswith(":*)") or perm.endswith(" *)")):
            entries = result.setdefault(script, [])
            entries.append(perm)
    return result


def is_subsumed(perm: str, canonical_map: dict[str, list[str]]) -> bool:
    """Check if a permission is subsumed by a canonical wildcard."""
    script = extract_script_base(perm)
    if script is None:
        return False
    return script in canonical_map


def is_old_validate_ci(perm: str) -> bool:
    """Check if this is an old-path validate_ci.sh reference."""
    m = OLD_VALIDATE_RE.match(perm)
    if not m:
        return False
    path = m.group(2)
    return (
        "validate_and_push" not in path
        and ("validate_ci.sh" in path or ".github/scripts/" in path)
    )


# Shell control flow fragments that are never valid standalone permissions
CONTROL_FLOW_RE = re.compile(
    r"^Bash\((while |do |done\)|fi\)|then\)|else\)|elif )"
)

# One-off debug commands
DEBUG_RE = re.compile(
    r'^Bash\(echo "(?:exit|EXIT): \$\?"|^Bash\(grep "expected'
)

# Pointless literals and commands
POINTLESS_RE = re.compile(
    r"^Bash\((1|exit \d+|/dev/null|bash --version|claude --version)\)$"
)

# Permissions with embedded UUIDs
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Permissions with embedded commit hashes (40-char hex after a space)
COMMIT_HASH_RE = re.compile(r" [0-9a-f]{40}(?:[)\s]|$)")

# Permissions referencing specific /tmp or /private/tmp files (not dirs)
TMP_FILE_RE = re.compile(
    r"^(?:Bash|Read)\((?:/private)?/tmp/(?:claude/)?[^\s)]*(?:\.(?:md|rs|txt|log|json)|/[0-9a-f-]{36})"
)

# Permissions referencing /var/folders (macOS temp)
VAR_FOLDERS_RE = re.compile(r"(?:/private)?/var/folders/")


def classify_junk(perm: str) -> str | None:
    """Return a junk reason string if the permission is nonsensical, else None."""
    if CONTROL_FLOW_RE.match(perm):
        return "control flow fragment"
    if DEBUG_RE.match(perm):
        return "one-off debug command"
    if POINTLESS_RE.match(perm):
        return "pointless literal"
    if UUID_RE.search(perm):
        return "contains UUID"
    if COMMIT_HASH_RE.search(perm):
        return "contains commit hash"
    if TMP_FILE_RE.match(perm):
        return "references tmp file"
    if VAR_FOLDERS_RE.search(perm):
        return "references /var/folders"
    return None


def extract_allow_list(data: dict[str, object]) -> list[str]:
    """Safely extract the permissions.allow list from settings data."""
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        return []
    allow: object = perms.get("allow")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(allow, list):
        return []
    return [str(p) for p in allow]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def process_project(
    project_dir: Path,
    canonical_perms: list[str],
    canonical_map: dict[str, list[str]],
    canonical_set: set[str],
    dry_run: bool,
) -> Report:
    """Process a single project."""
    settings_path = project_dir / ".claude" / "settings.local.json"

    if not settings_path.exists():
        if not dry_run:
            try:
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                save_json(settings_path, {"permissions": {"allow": list(canonical_perms)}})
            except PermissionError:
                return Report(project=project_dir.name, action="skip")
        return Report(
            project=project_dir.name,
            action="create",
            added=len(canonical_perms),
        )

    data = load_json(settings_path)
    existing = extract_allow_list(data)

    removed: list[str] = []
    kept: list[str] = []
    old_removed: list[str] = []
    junk_removed: list[tuple[str, str]] = []

    for perm in existing:
        if perm in canonical_set:
            continue
        if is_subsumed(perm, canonical_map):
            removed.append(perm)
        elif is_old_validate_ci(perm):
            old_removed.append(perm)
        else:
            junk_reason = classify_junk(perm)
            if junk_reason is not None:
                junk_removed.append((perm, junk_reason))
            else:
                kept.append(perm)

    # Final list: canonical entries first, then project-specific
    new_perms: list[str] = list(canonical_perms)
    for perm in kept:
        if perm not in canonical_set:
            new_perms.append(perm)

    # Rebuild settings preserving non-permissions keys (like prefersReducedMotion)
    new_data: dict[str, object] = {}
    for key, val in data.items():
        if key != "permissions":
            new_data[key] = val

    # Rebuild permissions preserving non-allow keys (like deny)
    raw_perms = data.get("permissions")
    new_perms_dict: dict[str, object] = {"allow": new_perms}
    if isinstance(raw_perms, dict):
        old_perms: dict[str, object] = {str(k): v for k, v in raw_perms.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        for key in old_perms:
            if key != "allow":
                new_perms_dict[key] = old_perms[key]
    new_data["permissions"] = new_perms_dict

    added_canonical = [p for p in canonical_perms if p not in set(existing)]

    if not dry_run:
        try:
            save_json(settings_path, new_data)
        except PermissionError:
            return Report(project=project_dir.name, action="skip")

    return Report(
        project=project_dir.name,
        action="update",
        removed_subsumed=removed,
        removed_old_paths=old_removed,
        removed_junk=junk_removed,
        added_canonical=added_canonical,
        kept_project_specific=len(kept),
        total_before=len(existing),
        total_after=len(new_perms),
    )


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--apply" not in sys.argv

    if dry_run:
        print("=== DRY RUN (pass --apply to write changes) ===\n")

    canonical_data = load_json(TEMPLATE)
    canonical_perms = extract_allow_list(canonical_data)
    canonical_set = set(canonical_perms)
    canonical_map = build_canonical_map(canonical_perms)

    print(f"Canonical template: {len(canonical_perms)} permissions")
    print(f"Canonical wildcards covering scripts: {len(canonical_map)}\n")

    all_projects = sorted(
        p for p in RUST_DIR.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )

    if args:
        filter_set = set(args)
        projects = [p for p in all_projects if p.name in filter_set]
        missing = filter_set - {p.name for p in projects}
        if missing:
            print(f"Warning: not found in ~/rust/: {', '.join(sorted(missing))}\n")
    else:
        projects = all_projects

    total_removed = 0
    total_added = 0

    for project_dir in projects:
        report = process_project(
            project_dir, canonical_perms, canonical_map, canonical_set, dry_run
        )

        if report.action == "skip":
            print(f"  {report.project}: SKIP (permission denied)")
            continue

        if report.action == "create":
            print(f"  {report.project}: CREATE ({report.added} canonical permissions)")
            total_added += report.added
            continue

        changes = (
            len(report.removed_subsumed)
            + len(report.removed_old_paths)
            + len(report.removed_junk)
            + len(report.added_canonical)
        )
        if changes == 0:
            print(f"  {report.project}: no changes ({report.total_before} entries)")
            continue

        print(f"  {report.project}: {report.total_before} -> {report.total_after} entries")

        if report.removed_subsumed:
            print("    REMOVED (subsumed by canonical wildcard):")
            for r in report.removed_subsumed:
                print(f"      - {r}")
            total_removed += len(report.removed_subsumed)

        if report.removed_old_paths:
            print("    REMOVED (old script paths):")
            for r in report.removed_old_paths:
                print(f"      - {r}")
            total_removed += len(report.removed_old_paths)

        if report.removed_junk:
            print("    REMOVED (junk):")
            for perm, reason in report.removed_junk:
                print(f"      - {perm}  [{reason}]")
            total_removed += len(report.removed_junk)

        if report.added_canonical:
            print("    ADDED (canonical defaults):")
            for a in report.added_canonical:
                print(f"      + {a}")
            total_added += len(report.added_canonical)

        print(f"    KEPT: {report.kept_project_specific} project-specific entries")

    print(f"\n{'=' * 60}")
    print(f"Total removed: {total_removed}")
    print(f"Total added:   {total_added}")
    if dry_run:
        print("\nThis was a dry run. Pass --apply to write changes.")


if __name__ == "__main__":
    main()
