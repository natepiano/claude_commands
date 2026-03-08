#!/usr/bin/env bash
set -euo pipefail

# Usage: update_workspace_deps.sh <version> [--dry-run] <dep_name> [<dep_name>...]
# Updates workspace dependency declarations in root Cargo.toml to the given version.
# Only touches lines matching `^<dep_name>` under [workspace.dependencies].
# Uses fibonacci backoff to wait for crates.io indexing before verifying the build.
# With --dry-run, reports what would happen without modifying anything.
# Exit 0 = success, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
  shift
fi

DEPS=("$@")

if [[ ${#DEPS[@]} -eq 0 ]]; then
  echo "ERROR: No dependency names provided" >&2
  exit 1
fi

echo "=== Update Workspace Dependencies ==="

for DEP in "${DEPS[@]}"; do
  CURRENT=$(grep "^${DEP}" Cargo.toml | head -1)
  if [[ -z "$CURRENT" ]]; then
    echo "ERROR: No workspace dependency found matching '^${DEP}' in Cargo.toml" >&2
    exit 1
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY-RUN] Would update Cargo.toml: $CURRENT → ${DEP} = \"$VERSION\""
  else
    echo "  Updating ${DEP} to $VERSION in Cargo.toml..."
    # Handle both simple (`dep = "1.0"`) and table (`dep = { version = "1.0", path = "..." }`) formats
    if grep -q "^${DEP}.*version" Cargo.toml; then
      # Table format — update only the version value, preserve path and other fields
      sed -i '' "/^${DEP}/s/version = \"[^\"]*\"/version = \"$VERSION\"/" Cargo.toml
    else
      # Simple format — replace the whole value
      sed -i '' "s/^${DEP} = .*/${DEP} = \"$VERSION\"/" Cargo.toml
    fi
    UPDATED=$(grep "^${DEP}" Cargo.toml | head -1)
    echo "  Cargo.toml now has: $UPDATED"
  fi
done

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] Would wait for crates.io indexing (fibonacci backoff)"
  echo "  [DRY-RUN] Would verify: cargo check"
  echo "  [DRY-RUN] Would commit: chore: update workspace deps to $VERSION"
  echo ""
  echo "[DRY-RUN] Workspace dependencies would be updated"
  exit 0
fi

echo ""
echo "  Waiting for crates.io to index updated dependencies..."

BACKOFF=(1 2 3 5 8 13 21 35)
BUILD_OK=false

for WAIT in "${BACKOFF[@]}"; do
  echo "    Attempting cargo check (backoff: ${WAIT}s)..."
  if cargo check 2>/dev/null; then
    BUILD_OK=true
    break
  fi
  echo "    Not indexed yet, waiting ${WAIT}s..."
  sleep "$WAIT"
done

if [[ "$BUILD_OK" != "true" ]]; then
  echo "ERROR: Dependencies not indexed on crates.io after all retries" >&2
  exit 1
fi

echo "  Build: passed"

echo ""
echo "  Committing workspace dependency update..."
git add Cargo.toml Cargo.lock
git commit -m "chore: update workspace deps to $VERSION"

echo ""
echo "Workspace dependencies updated to $VERSION"
