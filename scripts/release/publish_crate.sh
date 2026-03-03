#!/usr/bin/env bash
set -euo pipefail

# Usage: publish_crate.sh <package_name> [--dry-run]
# Runs cargo publish for the given package.
# If --dry-run is passed, only runs the dry-run check.
# Exit 0 = published (or dry-run passed), Exit 1 = failure

PACKAGE="$1"
DRY_RUN="${2:-}"

echo "=== Publishing $PACKAGE ==="

echo "  Running dry-run..."
cargo publish --package "$PACKAGE" --dry-run
echo "  Dry-run: passed ✓"

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "  Dry-run mode — skipping actual publish"
  exit 0
fi

echo ""
echo "  Publishing to crates.io..."
cargo publish --package "$PACKAGE"
echo "  Published: $PACKAGE ✓"
