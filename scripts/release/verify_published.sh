#!/usr/bin/env bash
set -euo pipefail

# Usage: verify_published.sh <version> <crate_name> [<crate_name>...]
# Checks that every crate has exactly <version> published, not yanked, on crates.io.
# Exit 0 = all verified, Exit 1 = a version is missing or yanked
#
# Looks up the exact version rather than comparing against `max_version`: a
# patch on an older line (0.22.8 published after 0.23.0-rc.1) is never the
# crate's highest version, and neither is any release made after a prerelease
# of the next line.

VERSION="$1"
shift
CRATES=("$@")
ALL_OK=true

# crates.io's /api/v1 enforces a data-access policy that rejects requests
# without an identifying User-Agent (curl's default UA fails). Without this the
# call returns an errors object with no `.version`, so every crate reads as
# missing. See https://crates.io/data-access.
CRATES_IO_UA="natepiano-release-script (https://github.com/natepiano)"

echo "=== Verifying Published Versions ==="

for CRATE in "${CRATES[@]}"; do
  RESPONSE=$(curl -s -A "$CRATES_IO_UA" "https://crates.io/api/v1/crates/$CRATE/$VERSION")
  PUBLISHED=$(jq -r '.version.num // empty' <<<"$RESPONSE")
  # No `// empty` here: jq's `//` treats `false` as absent, which would read
  # every unyanked version as yanked.
  YANKED=$(jq -r '.version.yanked' <<<"$RESPONSE")
  if [[ "$PUBLISHED" == "$VERSION" && "$YANKED" == "false" ]]; then
    echo "  $CRATE: $PUBLISHED ✓"
  elif [[ "$PUBLISHED" == "$VERSION" ]]; then
    echo "  $CRATE: $VERSION is yanked ✗"
    ALL_OK=false
  else
    DETAIL=$(jq -r '.errors[0].detail // "no version record returned"' <<<"$RESPONSE")
    echo "  $CRATE: $VERSION not found ($DETAIL) ✗"
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
