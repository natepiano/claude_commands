#!/bin/bash

# build-check.sh - Check for cargo build warnings and errors, then format code
# Usage: build-check.sh [build-args...]
# 
# This script runs cargo build and filters output to show only warnings and errors,
# then runs cargo +nightly fmt to ensure consistent code formatting.
# It's designed to be used by Claude Code subagents to check build status without 
# requiring permission approval for the grep pipeline.

set -euo pipefail

# Run cargo build and capture both stdout and stderr
# Pass any additional arguments to cargo build
# Show warnings/errors with file location context (the --> lines)
cargo build "$@" 2>&1 | grep -A1 -E "warning:|error:|-->" || true

# Note: The '|| true' ensures the script doesn't fail if grep finds no matches
# (which happens when there are no warnings/errors - a good thing!)

# Run the nightly formatter
cargo +nightly fmt