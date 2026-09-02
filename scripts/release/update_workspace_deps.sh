#!/usr/bin/env bash
set -euo pipefail

# Usage: update_workspace_deps.sh <version> [--dry-run] [--edit-only] [--auto] [<dep_name>...]
# Updates workspace dependency declarations in root Cargo.toml to the given version.
# Only touches lines matching `^<dep_name>` under [workspace.dependencies].
# Uses fibonacci backoff to wait for crates.io indexing before verifying the build.
# With --dry-run, reports what would happen without modifying anything.
# With --edit-only, rewrites the declarations and exits — no crates.io wait, no
#   cargo check, no commit. Use before the dependency version exists on crates.io
#   (STEP 4 version bump, STEP 11 dev-version restore), where the caller folds
#   Cargo.toml into its own commit.
# With --auto, discovers the dependency names instead of taking them as arguments,
#   and cannot be combined with explicit names. A declaration qualifies when it is
#   an inline entry under [workspace.dependencies] carrying BOTH `path` and
#   `version`, the path is inside the workspace, and the crate at that path is
#   already at <version> — i.e. bump_versions.sh just bumped it. Path-only entries
#   state no requirement and cannot break `cargo update`; external paths and
#   unbumped or excluded members fail the version match. Run AFTER bump_versions.sh.
#   Finding nothing is success: the project has no such dependencies.
# Exit 0 = success, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
EDIT_ONLY=""
AUTO=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run)   DRY_RUN="true" ;;
    --edit-only) EDIT_ONLY="true" ;;
    --auto)      AUTO="true" ;;
    *) echo "ERROR: Unknown flag: $1" >&2; exit 1 ;;
  esac
  shift
done

DEPS=("$@")

# True when $1 (a dependency's value text) contains $2 as a TOML key. Guards
# against the key name appearing inside the dependency name or a path string.
has_key() {
  [[ "$1" =~ (^|[\{,[:space:]])$2[[:space:]]*= ]]
}

# Emit the names of inline [workspace.dependencies] entries that declare both a
# path and a version, where the path resolves to a workspace crate already at
# $VERSION. Those are exactly the declarations a version bump invalidates.
detect_deps() {
  local line value name dep_path target_version

  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue

    # Match `path`/`version` as keys in the value, not as substrings anywhere on
    # the line — a dep named e.g. `dep_noversion` contains "version" in its name.
    value="${line#*=}"
    has_key "$value" path || continue
    has_key "$value" version || continue

    name="${line%%=*}"
    name="${name// /}"
    [[ -n "$name" ]] || continue

    dep_path=$(sed -n 's/.*path[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$line")
    [[ -n "$dep_path" ]] || continue

    # Anything reachable only via `..` or an absolute path is outside the
    # workspace, so its version is not ours to rewrite.
    [[ "$dep_path" == /* || "$dep_path" == ..* ]] && continue
    [[ -f "$dep_path/Cargo.toml" ]] || continue

    target_version=$(awk '
      /^\[package\]/ { in_pkg = 1; next }
      /^\[/          { in_pkg = 0 }
      in_pkg && /^version[[:space:]]*=/ {
        if (match($0, /"[^"]*"/)) { print substr($0, RSTART + 1, RLENGTH - 2); exit }
      }
    ' "$dep_path/Cargo.toml")

    [[ "$target_version" == "$VERSION" ]] || continue

    printf '%s\n' "$name"
  done < <(awk '/^\[workspace\.dependencies\]/ { f = 1; next } /^\[/ { f = 0 } f' Cargo.toml)
}

if [[ "$AUTO" == "true" ]]; then
  if [[ ${#DEPS[@]} -gt 0 ]]; then
    echo "ERROR: --auto discovers dependency names; do not also pass them explicitly" >&2
    exit 1
  fi

  # The sub-table form spreads keys across lines, so the inline scan cannot see
  # it. Surface it rather than silently reporting a clean run.
  if grep -q '^\[workspace\.dependencies\.' Cargo.toml; then
    echo "WARNING: [workspace.dependencies.<name>] sub-table entries are not auto-detected." >&2
    echo "         Check these by hand and pass them explicitly if they need updating:" >&2
    grep -n '^\[workspace\.dependencies\.' Cargo.toml >&2
  fi

  while IFS= read -r detected; do
    [[ -n "$detected" ]] && DEPS+=("$detected")
  done < <(detect_deps)

  if [[ ${#DEPS[@]} -eq 0 ]]; then
    echo "=== Update Workspace Dependencies ==="
    echo "  No internal path dependencies pin a version of a crate bumped to $VERSION — nothing to update."
    exit 0
  fi

  echo "=== Update Workspace Dependencies ==="
  echo "  Auto-detected: ${DEPS[*]}"
elif [[ ${#DEPS[@]} -eq 0 ]]; then
  echo "ERROR: No dependency names provided (pass names, or --auto to discover them)" >&2
  exit 1
else
  echo "=== Update Workspace Dependencies ==="
fi

for DEP in "${DEPS[@]}"; do
  CURRENT=$(grep "^${DEP}" Cargo.toml | head -1)
  if [[ -z "$CURRENT" ]]; then
    echo "ERROR: No workspace dependency found matching '^${DEP}' in Cargo.toml" >&2
    exit 1
  fi

  # Table vs simple form, decided on the value text so a dep whose NAME contains
  # "version" is not mistaken for one carrying a `version` key.
  if has_key "${CURRENT#*=}" version; then TABLE_FORM="true"; else TABLE_FORM=""; fi

  if [[ "$DRY_RUN" == "true" ]]; then
    if [[ "$TABLE_FORM" == "true" ]]; then
      echo "  [DRY-RUN] Would update Cargo.toml: $CURRENT → version = \"$VERSION\" (other keys preserved)"
    else
      echo "  [DRY-RUN] Would update Cargo.toml: $CURRENT → ${DEP} = \"$VERSION\""
    fi
  else
    echo "  Updating ${DEP} to $VERSION in Cargo.toml..."
    # Handle both simple (`dep = "1.0"`) and table (`dep = { version = "1.0", path = "..." }`) formats
    if [[ "$TABLE_FORM" == "true" ]]; then
      # Table format — update only the version value, preserve path and other fields
      sed -i.portbak "/^${DEP}/s/version = \"[^\"]*\"/version = \"$VERSION\"/" Cargo.toml
      rm -f Cargo.toml.portbak
    else
      # Simple format — replace the whole value
      sed -i.portbak "s/^${DEP} = .*/${DEP} = \"$VERSION\"/" Cargo.toml
      rm -f Cargo.toml.portbak
    fi
    UPDATED=$(grep "^${DEP}" Cargo.toml | head -1)
    echo "  Cargo.toml now has: $UPDATED"
  fi
done

if [[ "$EDIT_ONLY" == "true" ]]; then
  echo ""
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY-RUN] Workspace dependency declarations would be updated (no crates.io wait, no commit)"
  else
    echo "Workspace dependency declarations updated to $VERSION"
    echo "  Not committed — stage Cargo.toml with the caller's commit."
  fi
  exit 0
fi

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
