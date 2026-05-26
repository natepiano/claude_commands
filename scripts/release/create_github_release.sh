#!/usr/bin/env bash
set -euo pipefail

# Usage: create_github_release.sh <version> <repo> <project_name> <notes_file> [--dry-run] [--package <name>]
# Creates a GitHub release using the gh CLI.
# Without --package: tag v<version>, title "<project_name> v<version>".
# With --package <name>: tag <package>-v<version>, title "<package> v<version>"
# (single-package release out of a multi-crate workspace).
# Must be run with dangerouslyDisableSandbox: true (gh has TLS issues in sandbox).
# Exit 0 = created, Exit 1 = failure

VERSION="$1"
REPO="$2"
PROJECT_NAME="$3"
NOTES_FILE="$4"
shift 4

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
  TITLE="${PACKAGE} v${VERSION}"
else
  TAG="v${VERSION}"
  TITLE="${PROJECT_NAME} ${TAG}"
fi

# A semver pre-release (anything after a hyphen, e.g. -rc.1, -beta.2) must be
# flagged so GitHub does not mark it as the latest stable release.
if [[ "$VERSION" == *-* ]]; then
  PRERELEASE="true"
else
  PRERELEASE="false"
fi

if [[ ! -f "$NOTES_FILE" ]]; then
  echo "ERROR: Notes file not found: $NOTES_FILE" >&2
  exit 1
fi

echo "=== Create GitHub Release ==="

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "  [DRY-RUN] Would create release:"
  echo "    Tag: $TAG"
  echo "    Repo: $REPO"
  echo "    Title: $TITLE"
  echo "    Prerelease: $PRERELEASE"
  echo "    Notes:"
  sed 's/^/      /' "$NOTES_FILE"
  echo ""
  echo "[DRY-RUN] GitHub release would be created"
  exit 0
fi

echo "  Creating release: $TAG on $REPO (prerelease: $PRERELEASE)"
if [[ "$PRERELEASE" == "true" ]]; then
  gh release create "$TAG" \
    --repo "$REPO" \
    --title "$TITLE" \
    --notes-file "$NOTES_FILE" \
    --prerelease
else
  gh release create "$TAG" \
    --repo "$REPO" \
    --title "$TITLE" \
    --notes-file "$NOTES_FILE"
fi

echo ""
echo "GitHub release created: $TAG ✓"
