#!/usr/bin/env bash
set -euo pipefail

# Usage: prepare_next_cycle.sh <released_version> <next_dev_version> [--dry-run] <cargo_toml> [<cargo_toml>...] -- <changelog> [<changelog>...]
# Checks out main, bumps versions to next -dev, adds [Unreleased] sections to changelogs.
# The -- separator divides cargo toml files from changelog files.
# With --dry-run, reports what would happen without modifying anything.
# Exit 0 = prepared, Exit 1 = failure

RELEASED_VERSION="$1"
shift
NEXT_DEV="$1"
shift

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
  shift
fi

# Split args on --
CARGO_FILES=()
CHANGELOG_FILES=()
PAST_SEPARATOR=false

for arg in "$@"; do
  if [[ "$arg" == "--" ]]; then
    PAST_SEPARATOR=true
    continue
  fi
  if [[ "$PAST_SEPARATOR" == "true" ]]; then
    CHANGELOG_FILES+=("$arg")
  else
    CARGO_FILES+=("$arg")
  fi
done

echo "=== Prepare Next Release Cycle ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] Would checkout main"
  echo "  [DRY-RUN] Would bump versions to $NEXT_DEV:"
  for FILE in "${CARGO_FILES[@]}"; do
    echo "    [DRY-RUN] $FILE → $NEXT_DEV"
  done
  echo "  [DRY-RUN] Would add [Unreleased] sections:"
  for FILE in "${CHANGELOG_FILES[@]}"; do
    echo "    [DRY-RUN] $FILE → add [Unreleased]"
  done
  echo "  [DRY-RUN] Would commit: chore: prepare for next release cycle ($NEXT_DEV)"
  echo "  [DRY-RUN] Would push main"
  echo ""
  echo "[DRY-RUN] Main would be prepared for next cycle: $NEXT_DEV"
  exit 0
fi

echo "  Checking out main..."
git checkout main

echo "  Bumping versions to $NEXT_DEV..."
for FILE in "${CARGO_FILES[@]}"; do
  sed -i '' "0,/^version[[:space:]]*=/{s/^version[[:space:]]*=.*/version = \"$NEXT_DEV\"/}" "$FILE"
  echo "    $FILE → $NEXT_DEV"
done

echo "  Adding [Unreleased] sections..."
TODAY=$(date +%Y-%m-%d)
for FILE in "${CHANGELOG_FILES[@]}"; do
  if [[ ! -f "$FILE" ]]; then
    echo "    Skipping $FILE (not found)"
    continue
  fi
  # Add [Unreleased] section above the released version header
  sed -i '' "s/## \[$RELEASED_VERSION\] - $TODAY/## [Unreleased]\n\n## [$RELEASED_VERSION] - $TODAY/" "$FILE"
  echo "    $FILE → added [Unreleased]"
done

echo ""
echo "  Staging files..."
git add "${CARGO_FILES[@]}" "${CHANGELOG_FILES[@]}" Cargo.lock 2>/dev/null || true

echo "  Committing..."
git commit -m "chore: prepare for next release cycle ($NEXT_DEV)"

echo "  Pushing main..."
git push origin main

echo ""
echo "Main prepared for next cycle: $NEXT_DEV ✓"
