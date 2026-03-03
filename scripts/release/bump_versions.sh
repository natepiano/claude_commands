#!/usr/bin/env bash
set -euo pipefail

# Usage: bump_versions.sh <version> [--dry-run] <cargo_toml> [<cargo_toml>...]
# Updates the [package] version field in each Cargo.toml from -dev to the release version.
# Only touches the `version = "..."` line under [package], not dependency declarations.
# With --dry-run, reports what would change without modifying files.
# Exit 0 = all bumped, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
  shift
fi

FILES=("$@")

for FILE in "${FILES[@]}"; do
  if [[ ! -f "$FILE" ]]; then
    echo "ERROR: File not found: $FILE" >&2
    exit 1
  fi

  # Read current version
  CURRENT=$(grep '^version' "$FILE" | head -1 | sed 's/.*"\(.*\)".*/\1/')

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY-RUN] $FILE: $CURRENT → $VERSION (would change)"
    continue
  fi

  echo "  $FILE: $CURRENT → $VERSION"

  # Replace only the first `version = "..."` line (the [package] version)
  # Uses awk instead of sed for macOS/BSD compatibility (0,/pattern/ is GNU-only)
  awk -v ver="$VERSION" '!done && /^version[[:space:]]*=/ { sub(/=.*/, "= \"" ver "\""); done=1 } 1' "$FILE" > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"

  # Verify it took
  NEW=$(grep '^version' "$FILE" | head -1 | sed 's/.*"\(.*\)".*/\1/')
  if [[ "$NEW" != "$VERSION" ]]; then
    echo "ERROR: Version bump failed for $FILE — expected $VERSION, got $NEW" >&2
    exit 1
  fi
done

echo ""
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[DRY-RUN] All versions would be bumped to $VERSION"
else
  echo "All versions bumped to $VERSION"
fi
