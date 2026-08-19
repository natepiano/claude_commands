#!/usr/bin/env bash
set -euo pipefail

# Usage: pre_release_checks.sh
# Runs all pre-release quality checks from the project root.
# Must be on main branch (or a hotfix branch) with clean working directory.
# Exit 0 = all checks pass, Exit 1 = failure (prints reason to stderr)

echo "=== Git Status Check ==="

BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "  Branch: $BRANCH ✓"

STATUS=$(git status --porcelain)
if [[ -n "$STATUS" ]]; then
  echo "ERROR: Working directory has uncommitted changes:" >&2
  echo "$STATUS" >&2
  exit 1
fi
echo "  Working directory: clean ✓"

echo "  Fetching from origin..."
git fetch origin

echo ""
echo "=== Quality Checks ==="

# the lint CLI is the single bottom layer for cargo invocations; as a release
# gate, LINT_CONFIG_FORCE=1 keeps config/lint.conf toggles from skipping steps
LINT_CMD="$HOME/.claude/scripts/lint/lint"

echo "  Running clippy..."
env LINT_CONFIG_FORCE=1 "$LINT_CMD" clippy
echo "  Clippy: passed ✓"

echo ""
echo "  Running cargo build..."
cargo build --all
echo "  Build: passed ✓"

echo ""
echo "  Running cargo nextest..."
"$LINT_CMD" nextest --all
echo "  Tests: passed ✓"

echo ""
echo "  Running cargo fmt..."
env LINT_CONFIG_FORCE=1 "$LINT_CMD" fmt
echo "  Format: passed ✓"

echo ""
echo "All pre-release checks passed."
