#!/usr/bin/env bash
set -euo pipefail

# Usage: pin_path_deps.sh [--dry-run] <dep>=<version> [<dep>=<version>...]
#
# Pins path-only workspace dependencies in root Cargo.toml to a published
# crates.io version so `cargo publish` accepts them (path-only deps have no
# version requirement and cannot be published).
#
# Each <dep>=<version> rewrites the `^<dep> = ...` line under
# [workspace.dependencies] to the plain form `<dep> = "<version>"`, dropping
# the `path` key. The local path crate's version need not satisfy <version>:
# the path is removed, so the dependency resolves entirely from crates.io.
#
# Run this ONLY on a fire-and-forget release branch, just before publishing.
# It commits the pin (cargo publish requires a clean tree). The base branch is
# never modified, so it keeps the path dependency by construction — no restore.
#
# With --dry-run, reports what would change without modifying anything.
# Exit 0 = success, Exit 1 = failure

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
  shift
fi

PINS=("$@")

if [[ ${#PINS[@]} -eq 0 ]]; then
  echo "ERROR: No <dep>=<version> pins provided" >&2
  exit 1
fi

echo "=== Pin Workspace Path Dependencies ==="

for PIN in "${PINS[@]}"; do
  DEP="${PIN%%=*}"
  VERSION="${PIN#*=}"
  if [[ -z "$DEP" || -z "$VERSION" || "$DEP" == "$PIN" ]]; then
    echo "ERROR: Invalid pin '$PIN' — expected <dep>=<version>" >&2
    exit 1
  fi

  CURRENT=$(grep "^${DEP} " Cargo.toml | head -1 || true)
  if [[ -z "$CURRENT" ]]; then
    echo "ERROR: No workspace dependency found matching '^${DEP} ' in Cargo.toml" >&2
    exit 1
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY-RUN] Would pin Cargo.toml: $CURRENT → ${DEP} = \"$VERSION\""
  else
    echo "  Pinning ${DEP} to \"$VERSION\" in Cargo.toml..."
    sed -i '' "s|^${DEP} = .*|${DEP} = \"$VERSION\"|" Cargo.toml
    echo "  Cargo.toml now has: $(grep "^${DEP} " Cargo.toml | head -1)"
  fi
done

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] Would run: cargo update --workspace"
  echo "  [DRY-RUN] Would commit: chore: pin workspace path deps for publish"
  echo ""
  echo "[DRY-RUN] Path dependencies would be pinned"
  exit 0
fi

echo ""
echo "  Updating Cargo.lock..."
cargo update --workspace

echo ""
echo "  Committing pin..."
git add Cargo.toml Cargo.lock
git commit -m "chore: pin workspace path deps for publish"

echo ""
echo "Workspace path dependencies pinned"
