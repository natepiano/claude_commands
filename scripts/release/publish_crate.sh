#!/usr/bin/env bash
set -euo pipefail

# Usage: publish_crate.sh <package_name> [--dry-run] [--allow-dirty]
# Runs cargo publish for the given package.
# If --dry-run is passed, only runs the dry-run check.
# --allow-dirty is for a dry-run release that pinned path-only workspace deps in
# place: that pin is deliberately uncommitted (there is no release branch to
# commit it to) and gets reverted right after, but cargo refuses a dirty tree
# without this flag. It is rejected outside dry-run mode — a real publish always
# has its pin committed on the release branch, so a dirty tree there means
# something unintended is about to ship.
# Exit 0 = published (or dry-run passed), Exit 1 = failure

PACKAGE="$1"
shift

DRY_RUN=""
ALLOW_DIRTY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)     DRY_RUN="--dry-run"; shift ;;
    --allow-dirty) ALLOW_DIRTY="--allow-dirty"; shift ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "$ALLOW_DIRTY" && -z "$DRY_RUN" ]]; then
  echo "ERROR: --allow-dirty is only valid with --dry-run" >&2
  exit 1
fi

echo "=== Publishing $PACKAGE ==="

echo "  Running dry-run..."
cargo publish --package "$PACKAGE" --dry-run ${ALLOW_DIRTY}
echo "  Dry-run: passed ✓"

if [[ -n "$DRY_RUN" ]]; then
  echo "  Dry-run mode — skipping actual publish"
  exit 0
fi

echo ""
echo "  Publishing to crates.io..."
cargo publish --package "$PACKAGE"
echo "  Published: $PACKAGE ✓"
