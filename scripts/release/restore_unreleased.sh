#!/usr/bin/env bash
set -euo pipefail

# Usage: restore_unreleased.sh <version> [--dry-run] [--base-branch <name>] <changelog> [<changelog>...]
# Checks out the base branch (default: main) and adds ## [Unreleased] sections
# above the version header in each changelog file. Commits and pushes.
# Exit 0 = success, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
BASE_BRANCH="main"
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    --base-branch) BASE_BRANCH="$2"; shift 2 ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

FILES=("$@")
TODAY=$(date +%Y-%m-%d)

echo "=== Restore [Unreleased] Sections ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] Would checkout $BASE_BRANCH"
  for FILE in "${FILES[@]}"; do
    echo "  [DRY-RUN] $FILE → add [Unreleased] above [$VERSION] - $TODAY"
  done
  echo "  [DRY-RUN] Would commit: chore: restore [Unreleased] sections after v$VERSION"
  echo ""
  echo "[DRY-RUN] [Unreleased] sections would be restored"
  exit 0
fi

echo "  Checking out $BASE_BRANCH..."
git checkout "$BASE_BRANCH"

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
git push origin "$BASE_BRANCH"

echo ""
echo "[Unreleased] sections restored ✓"
