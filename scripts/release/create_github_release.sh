#!/usr/bin/env bash
set -euo pipefail

# Usage: create_github_release.sh <version> <repo> <project_name> <notes_file> [--dry-run]
# Creates a GitHub release using the gh CLI.
# Must be run with dangerouslyDisableSandbox: true (gh has TLS issues in sandbox).
# Exit 0 = created, Exit 1 = failure

VERSION="$1"
REPO="$2"
PROJECT_NAME="$3"
NOTES_FILE="$4"
DRY_RUN="${5:-}"

TAG="v${VERSION}"

echo "=== Create GitHub Release ==="

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "  [DRY-RUN] Would create release:"
  echo "    Tag: $TAG"
  echo "    Repo: $REPO"
  echo "    Title: $PROJECT_NAME $TAG"
  echo "    Notes from: $NOTES_FILE"
  echo ""
  echo "[DRY-RUN] GitHub release would be created"
  exit 0
fi

echo "  Creating release: $TAG on $REPO"
gh release create "$TAG" \
  --repo "$REPO" \
  --title "$PROJECT_NAME $TAG" \
  --notes-file "$NOTES_FILE"

echo ""
echo "GitHub release created: $TAG ✓"
