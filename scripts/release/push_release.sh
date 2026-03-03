#!/usr/bin/env bash
set -euo pipefail

# Usage: push_release.sh <version> [--dry-run]
# Creates tag, pushes release branch and tag to origin.
# With --dry-run, reports what would happen without pushing.
# Exit 0 = pushed, Exit 1 = failure

VERSION="$1"
DRY_RUN="${2:-}"
TAG="v${VERSION}"
BRANCH="release-${VERSION}"

echo "=== Push Release ==="

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "  [DRY-RUN] Would create tag: $TAG"
  echo "  [DRY-RUN] Would push branch: $BRANCH"
  echo "  [DRY-RUN] Would push tag: $TAG"
  echo ""
  echo "[DRY-RUN] Release would be pushed: $BRANCH + $TAG"
  exit 0
fi

echo "  Creating tag: $TAG"
git tag "$TAG"

echo "  Pushing branch: $BRANCH"
git push -u origin "$BRANCH"

echo "  Pushing tag: $TAG"
git push origin "$TAG"

echo ""
echo "Release pushed: $BRANCH + $TAG ✓"
