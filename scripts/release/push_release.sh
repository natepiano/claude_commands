#!/usr/bin/env bash
set -euo pipefail

# Usage: push_release.sh <version> [--dry-run] [--package <name>]
# Creates tag, pushes release branch and tag to origin.
# Without --package: branch release-<version>, tag v<version>.
# With --package <name>: branch release-<package>-<version>, tag <package>-v<version>
# (single-package release out of a multi-crate workspace).
# With --dry-run, reports what would happen without pushing.
# Exit 0 = pushed, Exit 1 = failure

VERSION="$1"
shift

DRY_RUN=""
PACKAGE=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    --package) PACKAGE="$2"; shift 2 ;;
    *) echo "ERROR: unknown flag: $1" >&2; exit 1 ;;
  esac
done

if [[ -n "$PACKAGE" ]]; then
  TAG="${PACKAGE}-v${VERSION}"
else
  TAG="v${VERSION}"
fi

# Push the branch HEAD is on, not a name derived from the version: the tag below
# is created on HEAD, so deriving the branch separately lets the two point at
# different commits. Normal mode is unaffected -- create_release_branch.sh has
# already checked out release-<package>-<version>. Hotfix mode skips that step
# entirely and runs from whatever branch the fix was built on, so a derived name
# either does not exist or belongs to some other commit.
BRANCH=$(git rev-parse --abbrev-ref HEAD)

# All three release modes publish from a branch that is not the default one:
# normal cuts release-<version>, hotfix runs off an old tag, isolated runs off
# the future mainline. Tagging a release on the default branch is always a
# mistake, and before BRANCH came from HEAD it failed here by accident.
DEFAULT_BRANCH=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's|^origin/||' || true)
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
if [[ "$BRANCH" == "$DEFAULT_BRANCH" ]]; then
  echo "ERROR: refusing to push a release from the default branch ($BRANCH)." >&2
  echo "       Releases publish from a release, hotfix, or isolated branch." >&2
  exit 1
fi

echo "=== Push Release ==="

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "  [DRY-RUN] Would create tag: $TAG"
  echo "  [DRY-RUN] Would push branch: $BRANCH"
  echo "  [DRY-RUN] Would push tag: $TAG"
  echo ""
  echo "[DRY-RUN] Release would be pushed: $BRANCH + $TAG"
  exit 0
fi

echo "  Creating tag: $TAG"
git tag "$TAG"

echo "  Pushing branch: $BRANCH"
git push -u origin "$BRANCH"

echo "  Pushing tag: $TAG"
git push origin "$TAG"

echo ""
echo "Release pushed: $BRANCH + $TAG ✓"
