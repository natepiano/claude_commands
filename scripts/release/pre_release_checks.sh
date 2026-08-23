#!/usr/bin/env bash
set -euo pipefail

# Usage: pre_release_checks.sh [--package <name>]
# Runs all pre-release quality checks from the project root.
# Must be on main branch (or a hotfix branch) with clean working directory.
# Exit 0 = all checks pass, Exit 1 = failure (prints reason to stderr)
#
# --package <name> scopes the compile-and-test gate to one crate and the
# siblings its own targets pull in. Use it for a single-package release.
# `cargo publish` already verifies the packaged tarball by compiling it in
# isolation against registry dependencies, so building unrelated workspace
# members proves nothing about what ships — a 24-member workspace can spend
# most of its build on crates the released crate does not depend on.
#
# The scoped run still passes --all-targets, which is the part that matters:
# it builds the examples and tests, and those are where a published crate's
# path-only dev-dependencies live. Formatting stays workspace-wide either way
# — it compiles nothing, and the release commits to the base branch.
#
# What scoping gives up is reverse-dependency coverage: a change that breaks a
# sibling's use of this crate goes unseen here. That is a "is the base branch
# healthy" question rather than a "is this crate publishable" one; run without
# --package when you want both.

PACKAGE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --package)
      PACKAGE="${2:-}"
      if [[ -z "$PACKAGE" ]]; then
        echo "ERROR: --package requires a crate name" >&2
        exit 1
      fi
      shift 2
      ;;
    --package=*)
      PACKAGE="${1#*=}"
      if [[ -z "$PACKAGE" ]]; then
        echo "ERROR: --package requires a crate name" >&2
        exit 1
      fi
      shift
      ;;
    *)
      echo "ERROR: unknown argument '$1'" >&2
      echo "Usage: pre_release_checks.sh [--package <name>]" >&2
      exit 1
      ;;
  esac
done

# The single bottom layer for cargo invocation policy: run(), lint.conf gating,
# fmt_cargo, run_nextest, invoke_clippy, and the SwiftPM sandbox-failure
# detection all come from here. Sourcing it directly rather than going through
# the `lint` CLI is the documented pattern for a caller that picks its own
# scope — delegate/verify.sh does the same for its per-package rules.
# shellcheck source=/dev/null
source "$HOME/.claude/scripts/lint/invoke.sh"

# As a release gate, this keeps config/lint.conf toggles from skipping steps.
export LINT_CONFIG_FORCE=1

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
if [[ -n "$PACKAGE" ]]; then
  echo "=== Quality Checks (scoped to $PACKAGE) ==="
  SCOPE=(-p "$PACKAGE")
else
  echo "=== Quality Checks (whole workspace) ==="
  SCOPE=(--workspace)
fi

echo "  Running clippy..."
invoke_clippy "${SCOPE[@]}" --all-targets --all-features
echo "  Clippy: passed ✓"

echo ""
# No --all-targets here: the clippy step above already type-checks every
# target, so this exists to catch what only a real build catches — linking.
echo "  Running cargo build..."
run cargo build "${SCOPE[@]}"
echo "  Build: passed ✓"

echo ""
echo "  Running cargo nextest..."
run_nextest "${SCOPE[@]}"
echo "  Tests: passed ✓"

echo ""
# Formatting is workspace-wide even under --package: it compiles nothing, so
# the wider scope is free, and the release commits to the base branch.
echo "  Running cargo fmt..."
fmt_cargo --all
echo "  Format: passed ✓"

echo ""
if [[ -n "$PACKAGE" ]]; then
  echo "All pre-release checks passed (scoped to $PACKAGE)."
else
  echo "All pre-release checks passed."
fi
