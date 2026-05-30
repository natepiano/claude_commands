#!/usr/bin/env bash
set -euo pipefail

# Usage: verify_published.sh <version> <crate_name> [<crate_name>...]
# Checks that all crates show the expected version on crates.io.
# Exit 0 = all verified, Exit 1 = mismatch

VERSION="$1"
shift
CRATES=("$@")
ALL_OK=true

# crates.io's /api/v1 enforces a data-access policy that rejects requests
# without an identifying User-Agent (curl's default UA fails). Without this the
# call returns an errors object with no `.crate`, so `.crate.max_version`
# yields null and every crate reads as a version mismatch. See
# https://crates.io/data-access.
CRATES_IO_UA="cargo-mend-release (https://github.com/natepiano/cargo-mend)"

echo "=== Verifying Published Versions ==="

for CRATE in "${CRATES[@]}"; do
  PUBLISHED=$(curl -s -A "$CRATES_IO_UA" "https://crates.io/api/v1/crates/$CRATE" | jq -r '.crate.max_version')
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
