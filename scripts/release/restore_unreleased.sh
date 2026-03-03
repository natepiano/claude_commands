#!/usr/bin/env bash
set -euo pipefail

# Usage: restore_unreleased.sh <version> [--dry-run] <changelog> [<changelog>...]
# Checks out main and adds ## [Unreleased] sections above the version header
# in each changelog file. Commits and pushes.
# Exit 0 = success, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
  shift
fi

FILES=("$@")
TODAY=$(date +%Y-%m-%d)

echo "=== Restore [Unreleased] Sections ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] Would checkout main"
  for FILE in "${FILES[@]}"; do
    echo "  [DRY-RUN] $FILE → add [Unreleased] above [$VERSION] - $TODAY"
  done
  echo "  [DRY-RUN] Would commit: chore: restore [Unreleased] sections after v$VERSION"
  echo ""
  echo "[DRY-RUN] [Unreleased] sections would be restored"
  exit 0
fi

echo "  Checking out main..."
git checkout main

for FILE in "${FILES[@]}"; do
  if [[ ! -f "$FILE" ]]; then
    echo "  Skipping $FILE (not found)"
    continue
  fi
  sed -i '' "s/## \[$VERSION\] - $TODAY/## [Unreleased]\n\n## [$VERSION] - $TODAY/" "$FILE"
  echo "  $FILE → added [Unreleased]"
done

echo ""
echo "  Staging and committing..."
git add "${FILES[@]}"
git commit -m "chore: restore [Unreleased] sections after v$VERSION"
git push origin main

echo ""
echo "[Unreleased] sections restored ✓"
