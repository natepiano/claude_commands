#!/usr/bin/env bash
set -euo pipefail

# Usage: push_release.sh <version> [--dry-run] [--package <name>]
# Creates tag, pushes release branch and tag to origin.
# Without --package: branch release-<version>, tag v<version>.
# With --package <name>: branch release-<package>-<version>, tag <package>-v<version>
# (single-package release out of a multi-crate workspace).
# With --dry-run, reports what would happen without pushing.
# Exit 0 = pushed, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
PACKAGE=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    --package) PACKAGE="$2"; shift 2 ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "$PACKAGE" ]]; then
  TAG="${PACKAGE}-v${VERSION}"
  BRANCH="release-${PACKAGE}-${VERSION}"
else
  TAG="v${VERSION}"
  BRANCH="release-${VERSION}"
fi

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
