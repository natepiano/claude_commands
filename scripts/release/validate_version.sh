#!/usr/bin/env bash
set -euo pipefail

# Usage: validate_version.sh <version> <crate_name> [<crate_name>...]
# Validates version format, checks not already published, and verifies no gaps.
# Exit 0 = valid, Exit 1 = invalid (prints reason to stderr)

VERSION="$1"
shift
CRATES=("$@")

# --- Format check ---
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
  echo "ERROR: Invalid version format: $VERSION" >&2
  echo "Valid formats: X.Y.Z or X.Y.Z-rc.N" >&2
  exit 1
fi

# Parse requested version components
REQ_MAJOR="${VERSION%%.*}"
REQ_REMAINDER="${VERSION#*.}"
REQ_MINOR="${REQ_REMAINDER%%.*}"
REQ_PATCH_FULL="${REQ_REMAINDER#*.}"
REQ_PATCH="${REQ_PATCH_FULL%%-*}"

for CRATE in "${CRATES[@]}"; do
  echo "Checking $CRATE..."

  # Query crates.io
  RESPONSE=$(curl -s "https://crates.io/api/v1/crates/$CRATE")

  # Check if crate exists on crates.io
  if echo "$RESPONSE" | jq -e '.errors' > /dev/null 2>&1; then
    echo "  Crate not yet published on crates.io — first release"
    continue
  fi

  # Get all published versions
  VERSIONS=$(echo "$RESPONSE" | jq -r '.versions[].num' 2>/dev/null)

  # Check exact match — already published?
  if echo "$VERSIONS" | grep -qx "$VERSION"; then
    echo "ERROR: $CRATE version $VERSION is already published on crates.io" >&2
    exit 1
  fi

  # Get max version (non-prerelease) for gap checking
  MAX_VERSION=$(echo "$RESPONSE" | jq -r '.crate.max_version' 2>/dev/null)

  if [[ -z "$MAX_VERSION" || "$MAX_VERSION" == "null" ]]; then
    echo "  No published versions found — first release"
    continue
  fi

  echo "  Current max version: $MAX_VERSION"

  # Parse max version components
  MAX_MAJOR="${MAX_VERSION%%.*}"
  MAX_REMAINDER="${MAX_VERSION#*.}"
  MAX_MINOR="${MAX_REMAINDER%%.*}"
  MAX_PATCH_FULL="${MAX_REMAINDER#*.}"
  MAX_PATCH="${MAX_PATCH_FULL%%-*}"

  # --- Gap check ---
  # Same major.minor line: patch must be max_patch + 1
  if [[ "$REQ_MAJOR" == "$MAX_MAJOR" && "$REQ_MINOR" == "$MAX_MINOR" ]]; then
    EXPECTED_PATCH=$((MAX_PATCH + 1))
    if [[ "$REQ_PATCH" -ne "$EXPECTED_PATCH" && -z "${VERSION##*-rc.*}" ]]; then
      # RC versions can target any not-yet-released patch
      :
    elif [[ "$REQ_PATCH" -ne "$EXPECTED_PATCH" && "$REQ_PATCH" -ne "$MAX_PATCH" ]]; then
      echo "ERROR: $CRATE version gap — expected patch $MAX_MAJOR.$MAX_MINOR.$EXPECTED_PATCH, got $VERSION" >&2
      exit 1
    fi
  # New minor: must be max_minor + 1, patch must be 0
  elif [[ "$REQ_MAJOR" == "$MAX_MAJOR" && "$REQ_MINOR" -gt "$MAX_MINOR" ]]; then
    EXPECTED_MINOR=$((MAX_MINOR + 1))
    if [[ "$REQ_MINOR" -ne "$EXPECTED_MINOR" ]]; then
      echo "ERROR: $CRATE version gap — expected minor $MAX_MAJOR.$EXPECTED_MINOR.0, got $VERSION" >&2
      exit 1
    fi
    if [[ "$REQ_PATCH" -ne 0 ]]; then
      echo "ERROR: $CRATE new minor version must start at patch 0, got $VERSION" >&2
      exit 1
    fi
  # New major: must be max_major + 1
  elif [[ "$REQ_MAJOR" -gt "$MAX_MAJOR" ]]; then
    EXPECTED_MAJOR=$((MAX_MAJOR + 1))
    if [[ "$REQ_MAJOR" -ne "$EXPECTED_MAJOR" ]]; then
      echo "ERROR: $CRATE version gap — expected major $EXPECTED_MAJOR.0.0, got $VERSION" >&2
      exit 1
    fi
  # Backport to older line: check no gap within that line
  elif [[ "$REQ_MAJOR" -lt "$MAX_MAJOR" || ("$REQ_MAJOR" == "$MAX_MAJOR" && "$REQ_MINOR" -lt "$MAX_MINOR") ]]; then
    # Find the max patch in the target major.minor line
    MAX_LINE_PATCH=$(echo "$VERSIONS" | grep "^${REQ_MAJOR}\.${REQ_MINOR}\." | grep -v -- '-' | sed "s/^${REQ_MAJOR}\.${REQ_MINOR}\.//" | sort -n | tail -1)
    if [[ -n "$MAX_LINE_PATCH" ]]; then
      EXPECTED_PATCH=$((MAX_LINE_PATCH + 1))
      if [[ "$REQ_PATCH" -ne "$EXPECTED_PATCH" ]]; then
        echo "ERROR: $CRATE version gap in ${REQ_MAJOR}.${REQ_MINOR}.x line — expected patch $EXPECTED_PATCH, got $REQ_PATCH" >&2
        exit 1
      fi
    fi
  fi

  echo "  OK — $VERSION is valid for $CRATE"
done

echo ""
echo "Version validation passed: $VERSION"
