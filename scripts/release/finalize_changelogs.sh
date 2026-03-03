#!/usr/bin/env bash
set -euo pipefail

# Usage: finalize_changelogs.sh <version> [--dry-run] <changelog> [<changelog>...]
# Replaces ## [Unreleased] with ## [version] - YYYY-MM-DD in each changelog.
# With --dry-run, reports what would change without modifying files.
# Exit 0 = all finalized, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
  shift
fi

FILES=("$@")
TODAY=$(date +%Y-%m-%d)

for FILE in "${FILES[@]}"; do
  if [[ ! -f "$FILE" ]]; then
    echo "ERROR: File not found: $FILE" >&2
    exit 1
  fi

  if ! grep -q '## \[Unreleased\]' "$FILE"; then
    echo "ERROR: No [Unreleased] section found in $FILE" >&2
    exit 1
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY-RUN] $FILE: [Unreleased] → [$VERSION] - $TODAY (would change)"
    continue
  fi

  echo "  $FILE: [Unreleased] → [$VERSION] - $TODAY"
  sed -i '' "s/## \[Unreleased\]/## [$VERSION] - $TODAY/" "$FILE"

  # Verify it took
  if grep -q '## \[Unreleased\]' "$FILE"; then
    echo "ERROR: Finalization failed for $FILE — [Unreleased] still present" >&2
    exit 1
  fi

  if ! grep -q "## \[$VERSION\] - $TODAY" "$FILE"; then
    echo "ERROR: Finalization failed for $FILE — version header not found" >&2
    exit 1
  fi
done

echo ""
if [[ "$DRY_RUN" == "true" ]]; then
  echo "[DRY-RUN] All changelogs would be finalized for $VERSION ($TODAY)"
else
  echo "All changelogs finalized for $VERSION ($TODAY)"
fi
