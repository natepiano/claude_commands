#!/usr/bin/env bash
set -euo pipefail

# Usage: next_patch_version.sh <crate_name>
#
# Prints the next patch version for <crate_name> on the line the current branch
# is on. Run from the workspace that contains the crate.
#
# The line comes from the crate's own version in this checkout, not from
# crates.io's max_version. max_version is the newest thing published from any
# branch: with 0.23.0-rc.1 out and main at 0.23.0-dev, incrementing it gives
# 0.23.1, which validate_version.sh accepts as the next 0.23 patch and which
# would ship main's older line above the prerelease.
#
# Rule: the highest published non-prerelease version at or below the branch
# version picks the major.minor line; the next version is one past the highest
# patch published on that line.
#   - A prerelease branch version (0.23.0-dev) excludes its own base: 0.23.0 is
#     not released yet, so the ceiling is everything below it -> 0.22.8 -> the
#     0.22 line -> 0.22.9.
#   - A release branch version (0.21.3 on a hotfix branch cut from v0.21.3) is
#     itself published, so it is included -> the 0.21 line. If 0.21.4 was
#     published since the branch was cut, the answer is 0.21.5, not a collision.
# Yanked versions count: a yanked number cannot be published again.
#
# stdout: the next version. stderr: how it was derived.
# Exit 0 = version printed, Exit 1 = cannot derive one (pass an explicit version)

CRATE="${1:?Usage: next_patch_version.sh <crate_name>}"
UA="User-Agent: natepiano-release-script (https://github.com/natepiano)"

# cargo metadata resolves `version.workspace = true` to the concrete version.
CURRENT=$(cargo metadata --no-deps --format-version 1 2>/dev/null \
  | jq -r --arg n "$CRATE" '.packages[] | select(.name == $n) | .version')
if [[ -z "$CURRENT" ]]; then
  echo "ERROR: $CRATE is not a package in this workspace" >&2
  exit 1
fi

BASE="${CURRENT%%-*}"
if [[ "$CURRENT" == *-* ]]; then
  INCLUDE_BASE=false
else
  INCLUDE_BASE=true
fi

# crates.io sparse index path: 1/x, 2/xy, 3/x/xyz, or xy/zw/name for 4+ chars.
NAME=$(printf '%s' "$CRATE" | tr '[:upper:]' '[:lower:]')
case "${#NAME}" in
  1) INDEX_PATH="1/$NAME" ;;
  2) INDEX_PATH="2/$NAME" ;;
  3) INDEX_PATH="3/${NAME:0:1}/$NAME" ;;
  *) INDEX_PATH="${NAME:0:2}/${NAME:2:2}/$NAME" ;;
esac

if ! INDEX=$(curl -sSfL -H "$UA" "https://index.crates.io/$INDEX_PATH"); then
  echo "ERROR: $CRATE is not published on crates.io — a first release needs an explicit version" >&2
  exit 1
fi

RELEASES=$(jq -r '.vers' <<<"$INDEX" | grep -v -- '-' | sort -V || true)

BELOW=""
while IFS= read -r V; do
  [[ -z "$V" ]] && continue
  if [[ "$V" == "$BASE" ]]; then
    [[ "$INCLUDE_BASE" == "true" ]] || continue
  elif [[ "$(printf '%s\n%s\n' "$V" "$BASE" | sort -V | head -1)" != "$V" ]]; then
    continue
  fi
  BELOW="$V"
done <<<"$RELEASES"

if [[ -z "$BELOW" ]]; then
  echo "ERROR: $CRATE has no published release at or below $CURRENT — pass an explicit version" >&2
  exit 1
fi

MAJOR="${BELOW%%.*}"
REST="${BELOW#*.}"
MINOR="${REST%%.*}"
LINE_HIGHEST=$(grep -E "^${MAJOR}\.${MINOR}\.[0-9]+$" <<<"$RELEASES" | tail -1)
PATCH="${LINE_HIGHEST##*.}"
NEXT="$MAJOR.$MINOR.$((PATCH + 1))"

echo "  $CRATE: branch version $CURRENT; line $MAJOR.$MINOR (highest published $LINE_HIGHEST); next patch: $NEXT" >&2
echo "$NEXT"
