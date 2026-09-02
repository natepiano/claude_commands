#!/usr/bin/env bash
set -euo pipefail

# Usage: pin_path_deps.sh [--dry-run] <dep>=<version> [<dep>=<version>...]
#        pin_path_deps.sh --restore
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
# Pin versions come from resolve_path_pins.sh, which reads the latest published
# release out of the crates.io sparse index.
#
# Normal mode runs on a fire-and-forget release branch, just before publishing.
# It commits the pin (cargo publish requires a clean tree). The base branch is
# never modified, so it keeps the path dependency by construction — no restore.
#
# --dry-run applies the same rewrite WITHOUT committing, so `cargo publish
# --dry-run` sees a publishable manifest. A dry-run release cuts no branch, so
# the edit lands on the working tree and MUST be undone with --restore once the
# dry-run publish finishes, pass or fail. To keep that undo safe, --dry-run
# refuses to start unless Cargo.toml and Cargo.lock are clean.
#
# Exit 0 = success, Exit 1 = failure

MODE="pin"
if [[ "${1:-}" == "--restore" ]]; then
  MODE="restore"
elif [[ "${1:-}" == "--dry-run" ]]; then
  MODE="dry-run"
  shift
fi

if [[ "$MODE" == "restore" ]]; then
  echo "=== Restore Workspace Path Dependencies ==="
  echo "  Reverting Cargo.toml and Cargo.lock to HEAD..."
  git checkout -- Cargo.toml Cargo.lock
  echo "  Cargo.toml now has: $(grep -E '^\S+ *= *\{ *path' Cargo.toml | head -3 | tr '\n' ' ')"
  echo ""
  echo "Path dependencies restored"
  exit 0
fi

PINS=("$@")

if [[ ${#PINS[@]} -eq 0 ]]; then
  echo "ERROR: No <dep>=<version> pins provided" >&2
  exit 1
fi

echo "=== Pin Workspace Path Dependencies ==="

if [[ "$MODE" == "dry-run" ]]; then
  DIRTY=$(git status --porcelain -- Cargo.toml Cargo.lock)
  if [[ -n "$DIRTY" ]]; then
    echo "ERROR: Cargo.toml or Cargo.lock has uncommitted changes:" >&2
    echo "$DIRTY" >&2
    echo "A dry-run pin edits both in place and undoes it with 'git checkout --'," >&2
    echo "which would discard that work. Commit or stash it first." >&2
    exit 1
  fi
  echo "  [DRY-RUN] Applying pins in place — undo with: pin_path_deps.sh --restore"
fi

for PIN in "${PINS[@]}"; do
  DEP="${PIN%%=*}"
  VERSION="${PIN#*=}"
  if [[ -z "$DEP" || -z "$VERSION" || "$DEP" == "$PIN" ]]; then
    echo "ERROR: Invalid pin '$PIN' — expected <dep>=<version>" >&2
    exit 1
  fi

  # taplo aligns the `=` with padding, so the key may be followed by several
  # spaces. Matching a single space rewrites nothing and reports success.
  MATCH="^${DEP}[[:space:]]*="
  CURRENT=$(grep -E "$MATCH" Cargo.toml | head -1 || true)
  if [[ -z "$CURRENT" ]]; then
    echo "ERROR: No workspace dependency found matching '${MATCH}' in Cargo.toml" >&2
    exit 1
  fi

  echo "  Pinning ${DEP} to \"$VERSION\" in Cargo.toml..."
  sed -i.portbak -E "s|${MATCH}.*|${DEP} = \"$VERSION\"|" Cargo.toml
  rm -f Cargo.toml.portbak
  PINNED=$(grep -E "$MATCH" Cargo.toml | head -1)
  echo "  Cargo.toml now has: $PINNED"
  if [[ "$PINNED" != "${DEP} = \"$VERSION\"" ]]; then
    echo "ERROR: Pin did not apply — expected '${DEP} = \"$VERSION\"', got '$PINNED'" >&2
    exit 1
  fi
done

echo ""
echo "  Updating Cargo.lock..."
cargo update --workspace

if [[ "$MODE" == "dry-run" ]]; then
  echo ""
  echo "  [DRY-RUN] Skipping commit — pin is uncommitted and must be restored"
  echo ""
  echo "[DRY-RUN] Path dependencies pinned in place"
  exit 0
fi

echo ""
echo "  Committing pin..."
git add Cargo.toml Cargo.lock
git commit -m "chore: pin workspace path deps for publish"

echo ""
echo "Workspace path dependencies pinned"
