#!/usr/bin/env bash
set -euo pipefail

# Usage: verify_published.sh <version> <crate_name> [<crate_name>...]
# Checks that all crates show the expected version on crates.io.
# Exit 0 = all verified, Exit 1 = mismatch

VERSION="$1"
shift
CRATES=("$@")
ALL_OK=true

echo "=== Verifying Published Versions ==="

for CRATE in "${CRATES[@]}"; do
  PUBLISHED=$(curl -s "https://crates.io/api/v1/crates/$CRATE" | jq -r '.crate.max_version')
  if [[ "$PUBLISHED" == "$VERSION" ]]; then
    echo "  $CRATE: $PUBLISHED ✓"
  else
    echo "  $CRATE: expected $VERSION, got $PUBLISHED ✗"
    ALL_OK=false
  fi
done

if [[ "$ALL_OK" != "true" ]]; then
  echo "" >&2
  echo "ERROR: Not all crates show version $VERSION on crates.io" >&2
  exit 1
fi

echo ""
echo "All crates verified at $VERSION"
