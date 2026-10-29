#!/usr/bin/env bash
# Load and prove the worktree's nix shell; a refusal fails the caller.
# Usage: direnv_allow.sh <worktree_path>

set -euo pipefail
exec "$(dirname "$0")/../lib/py" "$(dirname "$0")/../lib/nix_env.py" ensure "${1:-}" >/dev/null
