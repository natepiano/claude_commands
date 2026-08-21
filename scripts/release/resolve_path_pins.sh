#!/usr/bin/env bash
set -euo pipefail

# Usage: resolve_path_pins.sh <dep> [<dep>...]
#
# Resolves each path-only workspace dependency to the version it should be
# pinned to during publish, and verifies the local crate still matches what
# that version published.
#
# For each <dep>:
#   1. Reads the highest non-yanked, non-prerelease version from the crates.io
#      sparse index.
#   2. Downloads that published .crate and compares it file-by-file against the
#      set `cargo package --list` would ship from the local workspace member.
#   3. Emits `<dep>=<version>` on stdout for pin_path_deps.sh to consume.
#
# Generated and volatile files are excluded from the comparison: Cargo.toml and
# Cargo.lock (cargo rewrites them at package time), .cargo_vcs_info.json (records
# a commit hash), and CHANGELOG.md (a release finalizes it, then main restores an
# empty [Unreleased] section on top, so it always differs). Cargo.toml.orig is
# compared with the [package] version line removed, since main carries a -dev
# version by construction.
#
# Exit 0 = every dep is unchanged since its published version; pins on stdout
# Exit 2 = at least one dep has unpublished changes; pins still on stdout, and
#          the drift is reported on stderr. Publishing a dependent crate against
#          a stale dependency ships something CI never built.
# Exit 1 = failure

UA="User-Agent: natepiano-release-script"
DEPS=("$@")

if [[ ${#DEPS[@]} -eq 0 ]]; then
  echo "ERROR: No dependency names provided" >&2
  exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "=== Resolve Workspace Path Pins ===" >&2

# crates.io sparse index path: 1/x, 2/xy, 3/x/xyz, or xy/zw/name for 4+ chars.
index_path() {
  local name len
  name=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  len=${#name}
  case "$len" in
    1) printf '1/%s' "$name" ;;
    2) printf '2/%s' "$name" ;;
    3) printf '3/%s/%s' "${name:0:1}" "$name" ;;
    *) printf '%s/%s/%s' "${name:0:2}" "${name:2:2}" "$name" ;;
  esac
}

DRIFT=0

for DEP in "${DEPS[@]}"; do
  echo "" >&2
  echo "--- $DEP ---" >&2

  IDX="$WORK/$DEP.index"
  if ! curl -sSfL -H "$UA" "https://index.crates.io/$(index_path "$DEP")" -o "$IDX"; then
    echo "ERROR: $DEP is not published on crates.io — a path-only dep must be published before it can be pinned" >&2
    exit 1
  fi

  VERSION=$(jq -r 'select(.yanked == false) | .vers' "$IDX" \
    | grep -v -- '-' \
    | sort -V \
    | tail -1)

  if [[ -z "$VERSION" ]]; then
    echo "ERROR: $DEP has no published non-yanked release to pin to" >&2
    exit 1
  fi
  echo "  Latest published: $VERSION" >&2

  MANIFEST=$(cargo metadata --no-deps --format-version 1 2>/dev/null \
    | jq -r --arg n "$DEP" '.packages[] | select(.name == $n) | .manifest_path')
  if [[ -z "$MANIFEST" ]]; then
    echo "ERROR: $DEP is not a member of this workspace" >&2
    exit 1
  fi
  LOCAL_DIR=$(dirname "$MANIFEST")

  TARBALL="$WORK/$DEP.crate"
  if ! curl -sSfL -H "$UA" \
    "https://static.crates.io/crates/${DEP}/${DEP}-${VERSION}.crate" -o "$TARBALL"; then
    echo "ERROR: Could not download ${DEP}-${VERSION}.crate from crates.io" >&2
    exit 1
  fi

  PUB_DIR="$WORK/$DEP-pub"
  mkdir -p "$PUB_DIR"
  tar xzf "$TARBALL" -C "$PUB_DIR"
  PUB="$PUB_DIR/${DEP}-${VERSION}"
  if [[ ! -d "$PUB" ]]; then
    echo "ERROR: Unexpected layout in ${DEP}-${VERSION}.crate" >&2
    exit 1
  fi

  ( cd "$PUB" && find . -type f | sed 's|^\./||' | sort ) > "$WORK/$DEP.pub.list"
  cargo package --list --allow-dirty -p "$DEP" 2>/dev/null | sort > "$WORK/$DEP.loc.list"

  IGNORE='^(\.cargo_vcs_info\.json|Cargo\.lock|Cargo\.toml|Cargo\.toml\.orig|CHANGELOG\.md)$'
  ADDED=$(comm -13 "$WORK/$DEP.pub.list" "$WORK/$DEP.loc.list" | grep -vE "$IGNORE" || true)
  REMOVED=$(comm -23 "$WORK/$DEP.pub.list" "$WORK/$DEP.loc.list" | grep -vE "$IGNORE" || true)

  MODIFIED=""
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    if ! cmp -s "$PUB/$f" "$LOCAL_DIR/$f"; then
      MODIFIED="${MODIFIED}${f}"$'\n'
    fi
  done < <(comm -12 "$WORK/$DEP.pub.list" "$WORK/$DEP.loc.list" | grep -vE "$IGNORE" || true)

  # Manifest comparison, with the [package] version line dropped from both sides:
  # main always carries a -dev version, so that line differs by construction.
  strip_pkg_version() {
    awk '
      /^\[/ { section = $0 }
      { if (section == "[package]" && $0 ~ /^[[:space:]]*version[[:space:]]*=/) next; print }
    ' "$1"
  }
  MANIFEST_CHANGED=""
  if [[ -f "$PUB/Cargo.toml.orig" ]]; then
    strip_pkg_version "$PUB/Cargo.toml.orig" > "$WORK/$DEP.pub.toml"
    strip_pkg_version "$LOCAL_DIR/Cargo.toml" > "$WORK/$DEP.loc.toml"
    cmp -s "$WORK/$DEP.pub.toml" "$WORK/$DEP.loc.toml" || MANIFEST_CHANGED="Cargo.toml"
  fi

  if [[ -z "$ADDED" && -z "$REMOVED" && -z "$MODIFIED" && -z "$MANIFEST_CHANGED" ]]; then
    echo "  Unchanged since $VERSION — safe to pin" >&2
  else
    DRIFT=1
    echo "  UNPUBLISHED CHANGES since $VERSION:" >&2
    [[ -n "$MANIFEST_CHANGED" ]] && echo "    manifest: Cargo.toml" >&2
    [[ -n "$ADDED" ]] && { echo "    added:"; printf '      %s\n' $ADDED; } >&2
    [[ -n "$REMOVED" ]] && { echo "    removed:"; printf '      %s\n' $REMOVED; } >&2
    [[ -n "$MODIFIED" ]] && { echo "    modified:"; printf '      %s\n' $MODIFIED; } >&2
  fi

  printf '%s=%s\n' "$DEP" "$VERSION"
done

echo "" >&2
if [[ $DRIFT -eq 1 ]]; then
  echo "One or more path deps have changes that are not published." >&2
  echo "Pinning anyway publishes the dependent crate against an older API than this repo builds." >&2
  exit 2
fi

echo "All path pins resolved from crates.io" >&2
