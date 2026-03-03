#!/usr/bin/env bash
set -euo pipefail

# Usage: create_release_branch.sh <version> [--dry-run]
# Creates and checks out a release branch from current HEAD.
# With --dry-run, reports what would happen without creating the branch.
# Exit 0 = success, Exit 1 = failure

VERSION="$1"
DRY_RUN="${2:-}"
BRANCH="release-${VERSION}"

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "[DRY-RUN] Would create release branch: $BRANCH"
  echo "[DRY-RUN] Would checkout: $BRANCH"
  echo "[DRY-RUN] Current HEAD: $(git rev-parse --short HEAD)"
  exit 0
fi

echo "Creating release branch: $BRANCH"

git checkout -b "$BRANCH"

CURRENT=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT" != "$BRANCH" ]]; then
  echo "ERROR: Expected to be on $BRANCH, but on $CURRENT" >&2
  exit 1
fi

echo "On release branch: $BRANCH ✓"
