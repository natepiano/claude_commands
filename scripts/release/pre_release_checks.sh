#!/usr/bin/env bash
set -euo pipefail

# Usage: pre_release_checks.sh
# Runs all pre-release quality checks from the project root.
# Must be on main branch (or a hotfix branch) with clean working directory.
# Exit 0 = all checks pass, Exit 1 = failure (prints reason to stderr)

echo "=== Git Status Check ==="

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" == "main" ]]; then
  echo "  Branch: main ✓"
else
  echo "  Branch: $BRANCH (hotfix mode) ✓"
fi

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

echo "  Running cargo clippy..."
cargo clippy --all-targets --all-features -- -D warnings
echo "  Clippy: passed ✓"

echo ""
echo "  Running cargo build..."
cargo build --all
echo "  Build: passed ✓"

echo ""
echo "  Running cargo nextest..."
cargo nextest run --all
echo "  Tests: passed ✓"

echo ""
echo "  Running cargo fmt..."
cargo +nightly fmt --all
echo "  Format: passed ✓"

echo ""
echo "All pre-release checks passed."
